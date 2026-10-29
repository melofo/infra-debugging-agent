"""/investigate 接口测试：依赖注入换成假的 LLM，验证 HTTP 层的行为。"""

import pytest
from fastapi.testclient import TestClient

from app.api.chat import get_llm_client
from app.main import app
from app.scenario import load_scenario
from tests.fakes import SUBMIT_TOOL_NAME, VALID_REPORT_ARGS, ScriptedLLM, ToolCall, calls, final_text


def client_with(llm) -> TestClient:
    app.dependency_overrides[get_llm_client] = lambda: llm
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_investigate_returns_report_and_steps():
    llm = ScriptedLLM(
        [
            calls(ToolCall("get_ticket", {"ticket_id": "BUG-1042"})),
            calls(ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)),
            final_text(),
        ]
    )
    response = client_with(llm).post("/investigate", json={"scenario": "bug-1042", "mode": "loop"})

    assert response.status_code == 200
    body = response.json()
    assert body["report"]["incident_id"] == "BUG-1042"
    assert [s["tool"] for s in body["steps"]] == ["get_ticket", SUBMIT_TOOL_NAME]
    assert body["llm_calls"] == 3


@pytest.mark.parametrize("bad_name", ["../../.env", "BUG_1042", "a/b", "..", "UPPER"])
def test_investigate_rejects_path_traversal_and_bad_names(bad_name):
    # parametrize：同一个测试用多组输入各跑一遍，类似 JUnit 的 @ParameterizedTest。
    response = client_with(ScriptedLLM([])).post("/investigate", json={"scenario": bad_name})
    assert response.status_code == 422


def test_investigate_unknown_scenario_returns_404():
    response = client_with(ScriptedLLM([])).post("/investigate", json={"scenario": "does-not-exist"})
    assert response.status_code == 404


def test_sdk_mode_requires_gemini_provider():
    response = client_with(ScriptedLLM([])).post("/investigate", json={"mode": "sdk"})
    assert response.status_code == 400


def test_agent_failure_returns_500_without_leaking_details():
    # 模型一直不收尾 -> AgentError -> 500，而且响应里不暴露内部原因。
    endless = [calls(ToolCall("get_ticket", {"ticket_id": "BUG-1042"})) for _ in range(20)]
    response = client_with(ScriptedLLM(endless)).post("/investigate", json={})
    assert response.status_code == 500
    assert response.json()["detail"] == "investigation did not complete"


def test_load_scenario_itself_rejects_traversal():
    # 双重防御：就算有人绕过 HTTP 层直接调用，load_scenario 也会拒绝。
    with pytest.raises(ValueError):
        load_scenario("../../.env")
