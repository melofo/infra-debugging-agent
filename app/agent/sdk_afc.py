"""版本 B：使用 SDK 自带的 AFC（Automatic Function Calling），也就是"用框架"的方式。

和 loop.py 对比着读：
- 这里【没有 while 循环】，循环在 SDK 里面（models.py 的 generate_content）。
- 工具是普通 Python 函数；SDK 读取函数的"类型标注 + docstring"自动生成工具说明书。
  所以 docstring 不是注释，而是【写给模型看的说明】，改它会改变模型的行为。
- 代码短很多，但我们看不到中间的每一步（只能自己在函数里记录），
  也没办法在工具执行前统一插入权限检查、审批这类控制。
这就是"框架 vs 手写"的典型取舍：框架省事，手写可控。
"""

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
)
from app.agent.tools import ToolError, ToolRegistry
from app.llm.gemini import GeminiClient


class SdkAfcAgent:
    def __init__(self, llm: GeminiClient, registry: ToolRegistry, max_remote_calls: int = 8):
        self._llm = llm
        self._registry = registry
        self._max_remote_calls = max_remote_calls

    async def investigate(self, ticket_id: str) -> InvestigationResult:
        start = time.perf_counter()
        steps: list[StepRecord] = []
        submitted: list[SubmitRCAArgs] = []  # 用列表当"可修改的盒子"，让下面的函数能把结果存出来

        def run(tool: str, args: dict) -> dict:
            """所有工具函数共用：走同一个注册表，保留白名单和参数校验，并记录步骤。"""
            try:
                output = self._registry.execute(tool, args)
                steps.append(StepRecord(len(steps) + 1, tool, args, True, preview(output)))
                # SDK 要求工具返回值能放进 JSON 对象，所以列表要包一层。
                return output if isinstance(output, dict) else {"result": output}
            except ToolError as exc:
                steps.append(StepRecord(len(steps) + 1, tool, args, False, preview(exc)))
                return {"error": str(exc)}

        # 下面这些函数的【签名和 docstring】就是 SDK 生成工具说明书的依据。
        def get_ticket(ticket_id: str) -> dict:
            """Fetch a Jira ticket (title, description, service, severity, report time)."""
            return run("get_ticket", {"ticket_id": ticket_id})

        def search_logs(service: str, query: str = "", level: str | None = None, limit: int = 20) -> dict:
            """Search application logs of one service. Use it to find errors and when they started.

            Args:
                service: Service name, e.g. checkout-api.
                query: Case-insensitive text that must appear in the message. Empty matches all.
                level: Only this level: INFO, WARN or ERROR.
                limit: Max entries to return (1-50).
            """
            return run("search_logs", {"service": service, "query": query, "level": level, "limit": limit})

        def get_recent_deploys(service: str, limit: int = 5) -> dict:
            """List recent deployments of a service with commit and diff summary, newest first.

            Args:
                service: Service name, e.g. checkout-api.
                limit: Max deploys to return (1-20).
            """
            return run("get_recent_deploys", {"service": service, "limit": limit})

        def search_runbooks(query: str) -> dict:
            """Search internal runbooks for known symptoms and recommended checks or mitigations.

            Args:
                query: Keywords describing the symptom, e.g. connection pool timeout.
            """
            return run("search_runbooks", {"query": query})

        def submit_rca_report(summary: str, hypotheses: list[dict]) -> dict:
            """Finish the investigation by submitting the root cause analysis report. Call it once, last.

            Args:
                summary: 2-3 sentence summary of the most likely root cause.
                hypotheses: 1-5 hypotheses. Each is an object with: root_cause (string),
                    confidence (number 0-1), suggested_next_steps (list of strings), and evidence
                    (non-empty list of objects with: source one of jira/log/metric/trace/git/runbook,
                    summary string, reference string such as a commit sha or log message, and
                    optional observed_at ISO-8601 timestamp of when the evidence occurred).
            """
            # 注意：这里参数类型只能写成 list[dict]，SDK 不知道里面的精确结构，只能靠上面的文字描述。
            # 手写版本里模型拿到的是 Pydantic 生成的精确 JSON Schema，约束更强。这是一个真实的差别。
            try:
                submitted.append(SubmitRCAArgs(summary=summary, hypotheses=hypotheses))
            except ValidationError as exc:
                steps.append(StepRecord(len(steps) + 1, SUBMIT_TOOL_NAME, {}, False, "invalid report"))
                return {"error": f"invalid report: {exc.errors(include_url=False)}"}
            steps.append(StepRecord(len(steps) + 1, SUBMIT_TOOL_NAME, {}, True, "report submitted"))
            return {"status": "accepted"}

        response = await self._llm.generate_with_python_tools(
            prompt=f"Investigate ticket {ticket_id}.",
            functions=[get_ticket, search_logs, get_recent_deploys, search_runbooks, submit_rca_report],
            system=SYSTEM_PROMPT,
            max_remote_calls=self._max_remote_calls,
        )

        if not submitted:
            raise AgentError("model finished without submitting a valid report")

        return InvestigationResult(
            report=build_report(ticket_id, submitted[-1]),
            steps=steps,
            llm_calls=-1,  # SDK 不告诉我们内部发了几次请求，这也是框架"黑盒"的代价之一
            input_tokens=response.input_tokens,  # 只是最后一次请求的用量，不是总和
            output_tokens=response.output_tokens,
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
