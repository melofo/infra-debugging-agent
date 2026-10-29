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


VALID_REPORT_ARGS = {
    "summary": "Deploy 2.14.0 shrank the DB pool.",
    "hypotheses": [
        {
            "root_cause": "maximumPoolSize reduced from 50 to 5 in 2.14.0",
            "confidence": 0.9,
            "evidence": [
                {"source": "git", "summary": "pool size 50 -> 5", "reference": "commit 7be41d0"},
                {"source": "log", "summary": "Hikari connection not available", "reference": "checkout-api ERROR 14:02"},
            ],
            "suggested_next_steps": ["Roll back the pool size change"],
        }
    ],
}
