"""SDK 版本（AFC）Agent 的测试，以及 /investigate 的 502 路径。

SDK 的循环在真实的 Gemini 调用里面，我们不能联网测。所以这里写一个"模拟 AFC 的假 Gemini"：
它继承 GeminiClient（这样能通过 isinstance 检查），但不联网，而是像 SDK 那样
自己去调用我们传进来的 Python 函数。这样可以测试我们写的那些函数（工具包装、报告校验）。
"""

import pytest
from fastapi.testclient import TestClient

from app.agent.common import AgentError
from app.agent.sdk_afc import SdkAfcAgent
from app.agent.tools import build_investigation_tools
from app.api.chat import get_llm_client
from app.llm.base import LLMError, LLMResponse
from app.llm.gemini import GeminiClient
from app.main import app
from app.scenario import load_scenario
from tests.fakes import VALID_REPORT_ARGS


class FakeAfcGemini(GeminiClient):
    def __init__(self, script):
        # 故意不调用 super().__init__：那会创建真正的 SDK 客户端，需要 API key。
        self._script = script

    async def generate_with_python_tools(self, prompt, functions, system=None, max_remote_calls=8):
        by_name = {f.__name__: f for f in functions}  # 函数名 -> 函数，模拟 SDK 的 function_map
        self._script(by_name)
        return LLMResponse(text="", input_tokens=100, output_tokens=20, latency_ms=1)


@pytest.fixture
def registry():
    return build_investigation_tools(load_scenario("bug-1042"))


async def test_sdk_agent_records_steps_and_builds_report(registry):
    def script(tools):
        tools["get_ticket"](ticket_id="BUG-1042")
        tools["search_logs"](service="checkout-api", level="ERROR")
        tools["submit_rca_report"](**VALID_REPORT_ARGS)

    result = await SdkAfcAgent(FakeAfcGemini(script), registry).investigate("BUG-1042")

    assert [s.tool for s in result.steps] == ["get_ticket", "search_logs", "submit_rca_report"]
    assert result.report.hypotheses[0].confidence == 0.9
    assert result.llm_calls == -1  # SDK 不告诉我们内部发了几次请求


async def test_sdk_agent_tool_errors_are_returned_not_raised(registry):
    outputs = {}

    def script(tools):
        # 拼错的服务名：工具应返回 {"error": ...}（SDK 会把它交还给模型），而不是抛异常。
        outputs["bad"] = tools["search_logs"](service="payment-service")
        tools["submit_rca_report"](**VALID_REPORT_ARGS)

    result = await SdkAfcAgent(FakeAfcGemini(script), registry).investigate("BUG-1042")

    assert "known services" in outputs["bad"]["error"]
    assert result.steps[0].ok is False


async def test_sdk_agent_rejects_report_without_evidence(registry):
    def script(tools):
        bad = {"root_cause": "guess", "confidence": 0.5, "evidence": []}
        response = tools["submit_rca_report"](summary="s", hypotheses=[bad])
        assert "invalid report" in response["error"]

    # 模型没能提交合格的报告 -> 调查失败，而不是把不合格的报告当成功返回。
    with pytest.raises(AgentError):
        await SdkAfcAgent(FakeAfcGemini(script), registry).investigate("BUG-1042")


def test_sdk_mode_works_through_the_api():
    def script(tools):
        tools["submit_rca_report"](**VALID_REPORT_ARGS)

    app.dependency_overrides[get_llm_client] = lambda: FakeAfcGemini(script)
    try:
        response = TestClient(app).post("/investigate", json={"mode": "sdk"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["mode"] == "sdk"


def test_llm_failure_during_investigation_returns_502():
    class BrokenLLM:
        async def generate_with_tools(self, messages, tools, system=None):
            raise LLMError("provider down")

    app.dependency_overrides[get_llm_client] = lambda: BrokenLLM()
    try:
        response = TestClient(app).post("/investigate", json={})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 502
    assert response.json()["detail"] == "LLM provider error"  # 不泄露内部异常信息
