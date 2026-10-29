"""版本 A：我们自己手写的 agent loop。

这是整个项目最核心的文件。把它读懂，你就理解了 agent 是怎么工作的。
其他所有 agent 框架（LangGraph、Google ADK、OpenAI Agents SDK……）内部做的都是这件事，
只是加了更多功能。

流程（每一圈）：
  1. 把"完整对话历史 + 工具说明书"发给模型            ← 只发一次请求
  2. 模型回复：文字 / 想调用的工具
  3. 想调用工具 → 我们检查并执行，把结果追加到历史      ← 安全检查点就在这里
  4. 回到 1，直到这一轮结束（见下）

【多轮对话】一次用户发言触发的"一整个循环"叫一轮（turn）。对话历史（messages）由调用方保存，
每一轮接着它继续：用户追问时，模型能看到之前所有的工具结果，不用重新查询。
一轮的结束条件：模型用文字做了收尾（没有再调用工具）。具体来说：
  - 首次调查：模型调用 submit_rca_report 提交报告，工具结果交还后，它再用文字回应用户；
  - 追问：模型直接用文字回答。
"""

import logging
import time
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from app.agent.common import (
    SUBMIT_TOOL_NAME,
    SYSTEM_PROMPT,
    AgentError,
    InvestigationResult,
    StepRecord,
    SubmitRCAArgs,
    build_report,
    preview,
    submit_spec,
)
from app.agent.tools import ToolError, ToolRegistry
from app.llm.base import LLMClient, LLMError, Message, ToolResult

logger = logging.getLogger(__name__)

# 回调的类型：接收一个 StepRecord、返回协程（可以 await）的函数。类似 Java 的 Function 接口。
StepCallback = Callable[[StepRecord], Awaitable[None]]


class InvestigationAgent:
    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        max_steps: int = 8,
        system_prompt: str = SYSTEM_PROMPT,
    ):
        # 依赖通过构造器传入（构造器注入），测试时可以传假的 LLM，和 Spring 里的做法一致。
        self._llm = llm
        self._registry = registry
        self._max_steps = max_steps  # 注意：是【每一轮】的上限，不是整个会话的
        self._system_prompt = system_prompt

    async def investigate(self, ticket_id: str) -> InvestigationResult:
        """一次性调查（无会话）：新建一份空历史，跑一轮，必须产出报告。"""
        result = await self.run_turn([], f"Investigate ticket {ticket_id}.", ticket_id, require_report=True)
        if result.report is None:  # require_report=True 时不会发生，防御一下
            raise AgentError("investigation ended without a report")
        return result

    async def run_turn(
        self,
        messages: list[Message],
        user_text: str,
        ticket_id: str,
        require_report: bool,
        on_step: StepCallback | None = None,
    ) -> InvestigationResult:
        """在已有历史 messages 的基础上处理一次用户发言。

        注意 messages 会被【原地修改】：成功时追加了这一轮的所有消息；
        失败时我们把它恢复原样（回滚），见下面的说明。

        on_step：可选的回调（callback）。每完成一个步骤就调用它一次，用来实时推送进度（SSE）。
        循环本身不关心有没有人在听：不传就什么也不发生。这样"推送"和"调查"两件事互不耦合。
        """
        # 回滚点：这一轮开始前历史有多长。
        # 为什么必须回滚？假设模型已经说"我要调用 search_logs"（已追加进历史），
        # 但随后网络断了、没有拿到工具结果就失败。如果不清理，历史里会留下一个
        # "有函数调用、没有函数结果"的残缺片段，下一次请求会被模型厂商直接拒绝，
        # 这个会话就永远坏掉了。所以要么这一轮完整成功，要么就像没发生过：事务的思想。
        checkpoint = len(messages)
        try:
            return await self._run_turn(messages, user_text, ticket_id, require_report, on_step)
        except BaseException:
            # 用 BaseException 而不是 Exception：用户关掉网页时请求会被取消（CancelledError），
            # 它属于 BaseException，同样需要回滚。回滚完用 raise 原样把异常继续往上抛。
            del messages[checkpoint:]
            raise

    async def _run_turn(
        self,
        messages: list[Message],
        user_text: str,
        ticket_id: str,
        require_report: bool,
        on_step: StepCallback | None,
    ) -> InvestigationResult:
        start = time.perf_counter()
        # 工具说明书 = 调查工具 + 终止工具。
        tools = self._registry.specs() + [submit_spec()]
        messages.append(Message(role="user", text=user_text))
        steps: list[StepRecord] = []
        llm_calls = input_tokens = output_tokens = 0

        async def add_step(record: StepRecord) -> None:
            steps.append(record)
            if on_step is not None:
                await on_step(record)  # 实时通知外面："刚刚完成了这一步"

        def result(answer: str, report=None) -> InvestigationResult:
            return InvestigationResult(
                answer=answer,
                report=report,
                steps=steps,
                llm_calls=llm_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=int((time.perf_counter() - start) * 1000),
            )

        submitted = None  # 这一轮里成功提交的报告（可能在某一圈提交，之后还有一圈让模型做文字收尾）

        def finish_with_report() -> InvestigationResult:
            """给"已提交报告、但没能拿到模型文字收尾"的情况兜底：用报告摘要当答案。"""
            # 历史必须以 model 消息结尾（见下面的说明），所以补一条确认消息。
            messages.append(Message(role="model", text=f"Report submitted: {submitted.summary}"))
            return result(submitted.summary, report=submitted)

        # 最大步数是硬性护栏：防止模型陷入死循环，白白烧 token（钱）。
        for step in range(1, self._max_steps + 1):
            # 每一圈都把【完整历史】重发给模型（模型没有记忆）。
            # 这就是 token 增长的来源：对话越长、工具结果越多，每次请求越贵。
            try:
                response = await self._llm.generate_with_tools(messages, tools, system=self._system_prompt)
            except LLMError:
                if submitted is None:
                    raise  # 还没有任何成果：整轮失败，由 run_turn 回滚
                # 报告已经提交了，只是最后"文字收尾"这一次调用失败。别因此丢掉整份报告：
                # 用摘要兜底，成功返回。这是"部分成功也要保住已有成果"的思路。
                logger.warning("final answer call failed after report was submitted")
                return finish_with_report()
            llm_calls += 1
            input_tokens += response.input_tokens
            output_tokens += response.output_tokens

            # 把模型这一轮的输出追加进历史，下一圈它才"记得"自己说过什么。
            messages.append(
                Message(role="model", text=response.text, tool_calls=response.tool_calls, raw=response.raw)
            )

            if not response.tool_calls:
                if submitted is not None:
                    # 报告已提交，模型现在用文字回应用户的原话：这一轮结束。
                    # 这是 agent 循环最自然的结尾：工具结果交还给模型，模型说完话，才算完。
                    return result(response.text, report=submitted)
                if not require_report:
                    # 追问场景：模型直接用文字回答了，这一轮结束。
                    return result(response.text)
                # 首次调查：模型只回了文字，没提交报告。提醒它按规则收尾。
                messages.append(
                    Message(role="user", text=f"Finish by calling {SUBMIT_TOOL_NAME} with your findings.")
                )
                continue

            tool_results: list[ToolResult] = []
            for call in response.tool_calls:
                if submitted is not None and call.name == SUBMIT_TOOL_NAME:
                    # 同一轮里重复提交：只认最先收下的那份，历史里仍需要有对应结果。
                    tool_results.append(ToolResult(call.name, "skipped: report already submitted", is_error=True))
                    continue

                if call.name == SUBMIT_TOOL_NAME:
                    try:
                        args = SubmitRCAArgs.model_validate(call.args)
                    except ValidationError as exc:
                        # 报告不合格（例如假设没有证据）：把原因告诉模型，让它重写，而不是崩溃。
                        message = f"invalid report: {exc.errors(include_url=False)}"
                        tool_results.append(ToolResult(call.name, message, is_error=True))
                        await add_step(StepRecord(step, call.name, call.args, False, preview(message)))
                        continue
                    submitted = build_report(ticket_id, args)
                    tool_results.append(ToolResult(call.name, "report accepted"))
                    await add_step(StepRecord(step, call.name, call.args, True, "report submitted"))
                    continue

                # 普通调查工具：交给注册表执行（白名单 + 参数校验都在 registry 里）。
                try:
                    output = self._registry.execute(call.name, call.args)
                    tool_results.append(ToolResult(call.name, output))
                    await add_step(StepRecord(step, call.name, call.args, True, preview(output)))
                except ToolError as exc:
                    # 工具出错不让整个调查崩溃：错误文字回传给模型，它常常能自己改正。
                    tool_results.append(ToolResult(call.name, str(exc), is_error=True))
                    await add_step(StepRecord(step, call.name, call.args, False, preview(exc)))

            # 把所有工具结果作为一条 "tool" 消息追加，下一圈模型就能看到它们。
            messages.append(Message(role="tool", tool_results=tuple(tool_results)))

        if submitted is not None:
            # 报告在最后一圈才提交，没有余量再让模型文字收尾了：兜底返回，不丢报告。
            return finish_with_report()
        raise AgentError(f"turn did not finish within {self._max_steps} steps")
