"""测试共用的假对象。

之前 test_investigate_api.py 直接从 test_agent_loop.py 里 import 东西，
测试文件之间互相依赖是坏习惯（一个文件改了，另一个莫名其妙失败）。
共用的东西放到这种专门的"工具模块"里，测试文件都从这里 import。
"""

from app.agent.common import SUBMIT_TOOL_NAME  # noqa: F401  测试里会用到，一并从这里导出
from app.llm.base import LLMResponse, ToolCall  # noqa: F401


class ScriptedLLM:
    """按顺序返回预设的响应，并记录每次收到的对话历史，方便断言。"""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.seen_messages: list[list] = []

    async def generate(self, prompt, system=None):
        raise NotImplementedError

    async def generate_with_tools(self, messages, tools, system=None):
        self.seen_messages.append(list(messages))  # 复制一份，因为 loop 之后还会继续往原列表里追加
        return self._responses.pop(0)


def calls(*tool_calls: ToolCall, text: str = "") -> LLMResponse:
    return LLMResponse(text=text, input_tokens=10, output_tokens=5, latency_ms=1, tool_calls=tuple(tool_calls))


# 测试里的报告也必须通过【证据落地检查】（见 app/agent/grounding.py）：证据的出处要能在工具返回的数据里找到原文。
# 所以假模型必须先调用对应的工具，再引用工具真正返回的内容；不能再随手编一个 "commit 7be41d0"。
#
# VALID_REPORT_ARGS 只引用工单（来自 get_ticket）：最短的调查流程（只查工单就提交）也能用它。
JIRA_EVIDENCE = {
    "source": "jira",
    "summary": "Ticket reports intermittent HTTP 500s at checkout since about 14:00 UTC",
    "reference": "BUG-1042",
}
GIT_EVIDENCE = {"source": "git", "summary": "pool size 50 -> 5", "reference": "7be41d0"}
LOG_EVIDENCE = {
    "source": "log",
    "summary": "Hikari cannot hand out connections",
    "reference": "HikariPool-1 - Connection is not available, request timed out after 30000ms",
}


GET_TICKET = ToolCall("get_ticket", {"ticket_id": "BUG-1042"})  # 只要先成功查过工单，就能引用工单作为证据


def report_args(*evidence: dict) -> dict:
    return {
        "summary": "Deploy 2.14.0 shrank the DB pool.",
        "hypotheses": [
            {
                "root_cause": "maximumPoolSize reduced from 50 to 5 in 2.14.0",
                "confidence": 0.9,
                "evidence": list(evidence),
                "suggested_next_steps": ["Roll back the pool size change"],
            }
        ],
    }


VALID_REPORT_ARGS = report_args(JIRA_EVIDENCE)
# 引用了日志和发布记录：提交前必须已经成功调用过 search_logs 和 get_recent_deploys。
RICH_REPORT_ARGS = report_args(JIRA_EVIDENCE, GIT_EVIDENCE, LOG_EVIDENCE)


def final_text(text: str = "Investigation complete, see the report.") -> LLMResponse:
    """提交报告之后，模型用文字收尾的那一次响应。"""
    return calls(text=text)


class AutoLLM:
    """一个"无状态"的假模型：根据对话历史的最后一条消息，决定下一步说什么。

    不用预设剧本（ScriptedLLM 按顺序弹出响应），因为 Worker 测试里会有多个任务并发，
    各自的调用顺序是交错的，按顺序弹出就乱了。这里每次只看历史就能决定，并发也安全。
    """

    def __init__(self):
        self.calls = 0

    async def generate(self, prompt, system=None):
        raise NotImplementedError

    async def generate_with_tools(self, messages, tools, system=None) -> LLMResponse:
        self.calls += 1
        last = messages[-1]
        if last.role == "user":
            return calls(ToolCall("get_ticket", {"ticket_id": "BUG-1042"}))
        done = last.tool_results[0].name
        if done == "get_ticket":
            return calls(ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS))
        return final_text("Done.")
