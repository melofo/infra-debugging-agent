"""版本 A：我们自己手写的 agent loop。

这是整个项目最核心的文件。把它读懂，你就理解了 agent 是怎么工作的。
其他所有 agent 框架（LangGraph、Google ADK、OpenAI Agents SDK……）内部做的都是这件事，
只是加了更多功能。

流程（每一圈）：
  1. 把"完整对话历史 + 工具说明书"发给模型            ← 只发一次请求
  2. 模型回复：文字 / 想调用的工具
  3. 想调用工具 → 我们检查并执行，把结果追加到历史      ← 安全检查点就在这里
  4. 回到 1，直到模型调用 submit_rca_report，或超过最大步数
"""

import logging
import time

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
from app.llm.base import LLMClient, Message, ToolResult

logger = logging.getLogger(__name__)


class InvestigationAgent:
    def __init__(self, llm: LLMClient, registry: ToolRegistry, max_steps: int = 8):
        # 依赖通过构造器传入（构造器注入），测试时可以传假的 LLM，和 Spring 里的做法一致。
        self._llm = llm
        self._registry = registry
        self._max_steps = max_steps

    async def investigate(self, ticket_id: str) -> InvestigationResult:
        start = time.perf_counter()
        # 工具说明书 = 调查工具 + 终止工具。
        tools = self._registry.specs() + [submit_spec()]
        # 对话历史。模型没有记忆，我们每一圈都把整个列表重新发给它。
        messages = [Message(role="user", text=f"Investigate ticket {ticket_id}.")]
        # 过程中的统计先用普通变量累计，报告产出后再一次性组装结果对象。
        steps: list[StepRecord] = []
        llm_calls = input_tokens = output_tokens = 0

        # 最大步数是硬性护栏：防止模型陷入死循环，白白烧 token（钱）。
        for step in range(1, self._max_steps + 1):
            response = await self._llm.generate_with_tools(messages, tools, system=SYSTEM_PROMPT)
            llm_calls += 1
            input_tokens += response.input_tokens
            output_tokens += response.output_tokens

            # 把模型这一轮的输出追加进历史，下一圈它才"记得"自己说过什么。
            messages.append(
                Message(role="model", text=response.text, tool_calls=response.tool_calls, raw=response.raw)
            )

            if not response.tool_calls:
                # 模型只回了文字，没有调用任何工具，也没提交报告。提醒它按规则收尾。
                messages.append(
                    Message(role="user", text=f"Finish by calling {SUBMIT_TOOL_NAME} with your findings.")
                )
                continue

            tool_results: list[ToolResult] = []
            for call in response.tool_calls:
                if call.name == SUBMIT_TOOL_NAME:
                    try:
                        args = SubmitRCAArgs.model_validate(call.args)
                    except ValidationError as exc:
                        # 报告不合格（例如假设没有证据）：把原因告诉模型，让它重写，而不是崩溃。
                        message = f"invalid report: {exc.errors(include_url=False)}"
                        tool_results.append(ToolResult(call.name, message, is_error=True))
                        steps.append(StepRecord(step, call.name, call.args, False, preview(message)))
                        continue
                    steps.append(StepRecord(step, call.name, call.args, True, "report submitted"))
                    logger.info(
                        "investigation finished",
                        extra={"steps": step, "llm_calls": llm_calls, "ticket_id": ticket_id},
                    )
                    return InvestigationResult(
                        report=build_report(ticket_id, args),
                        steps=steps,
                        llm_calls=llm_calls,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        latency_ms=int((time.perf_counter() - start) * 1000),
                    )

                # 普通调查工具：交给注册表执行（白名单 + 参数校验都在 registry 里）。
                try:
                    output = self._registry.execute(call.name, call.args)
                    tool_results.append(ToolResult(call.name, output))
                    steps.append(StepRecord(step, call.name, call.args, True, preview(output)))
                except ToolError as exc:
                    # 工具出错不让整个调查崩溃：错误文字回传给模型，它常常能自己改正。
                    tool_results.append(ToolResult(call.name, str(exc), is_error=True))
                    steps.append(StepRecord(step, call.name, call.args, False, preview(exc)))

            # 把所有工具结果作为一条 "tool" 消息追加，下一圈模型就能看到它们。
            messages.append(Message(role="tool", tool_results=tuple(tool_results)))

        raise AgentError(f"investigation did not finish within {self._max_steps} steps")
