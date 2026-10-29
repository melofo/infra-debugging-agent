"""SSE 流式接口的测试。

SSE 返回的是文本流，每个事件形如：
    event: step
    data: {...json...}
    <空行>
parse_sse 把它解析回 (事件名, 数据) 的列表，方便断言。
"""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent.loop import InvestigationAgent
from app.agent.tools import build_investigation_tools
from app.api.chat import get_llm_client
from app.llm.base import LLMError
from app.main import app
from app.scenario import load_scenario
from app.sessions import SessionStore, get_session_store
from tests.fakes import SUBMIT_TOOL_NAME, VALID_REPORT_ARGS, ScriptedLLM, ToolCall, calls, final_text


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


@pytest.fixture
def store():
    fresh = SessionStore()
    app.dependency_overrides[get_session_store] = lambda: fresh
    yield fresh
    app.dependency_overrides.clear()


def use_llm(llm):
    app.dependency_overrides[get_llm_client] = lambda: llm


def investigation_script():
    return [
        calls(ToolCall("get_ticket", {"ticket_id": "BUG-1042"})),
        calls(ToolCall("search_logs", {"service": "checkout-api", "level": "ERROR"})),
        calls(ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)),
        final_text(),
    ]


def test_stream_emits_step_events_then_final_turn(store):
    use_llm(ScriptedLLM(investigation_script()))
    client = TestClient(app)
    session_id = client.post("/sessions", json={}).json()["id"]

    response = client.post(f"/sessions/{session_id}/messages/stream", json={"message": "go"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    events = parse_sse(response.text)
    # 三个步骤事件（依次是三个工具调用完成），最后一个 turn 事件带完整结果。
    assert [name for name, _ in events] == ["step", "step", "step", "turn"]
    assert [data["tool"] for name, data in events if name == "step"] == [
        "get_ticket",
        "search_logs",
        SUBMIT_TOOL_NAME,
    ]
    final = events[-1][1]
    assert final["report"]["incident_id"] == "BUG-1042"
    assert len(final["steps"]) == 3


def test_stream_result_is_stored_and_lock_released(store):
    use_llm(ScriptedLLM(investigation_script() + [calls(text="kpatel")]))
    client = TestClient(app)
    session_id = client.post("/sessions", json={}).json()["id"]
    client.post(f"/sessions/{session_id}/messages/stream", json={"message": "go"})

    session = store.get(session_id)
    assert not session.lock.locked()  # 流结束后锁必须释放
    assert len(session.turns) == 1

    # 同一个会话还能继续用普通接口追问。
    follow_up = client.post(f"/sessions/{session_id}/messages", json={"message": "who?"})
    assert follow_up.json()["answer"] == "kpatel"


def test_llm_failure_is_sent_as_error_event_and_rolled_back(store):
    class Broken(ScriptedLLM):
        async def generate_with_tools(self, messages, tools, system=None):
            raise LLMError("provider down")

    use_llm(Broken([]))
    client = TestClient(app)
    session_id = client.post("/sessions", json={}).json()["id"]

    response = client.post(f"/sessions/{session_id}/messages/stream", json={"message": "go"})

    # 推流时 HTTP 状态码已经是 200 了，错误只能放在事件里告诉客户端。
    assert response.status_code == 200
    events = parse_sse(response.text)
    assert events == [("error", {"status": 502, "detail": "LLM provider error"})]
    session = store.get(session_id)
    assert session.messages == []  # 回滚
    assert not session.lock.locked()


def test_stream_unknown_session_returns_404(store):
    use_llm(ScriptedLLM([]))
    response = TestClient(app).post("/sessions/nope/messages/stream", json={"message": "go"})
    assert response.status_code == 404


async def test_stream_to_busy_session_returns_409(store):
    release = asyncio.Event()

    class Slow(ScriptedLLM):
        async def generate_with_tools(self, messages, tools, system=None):
            await release.wait()
            return await super().generate_with_tools(messages, tools, system)

    use_llm(Slow([calls(ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)), final_text()]))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session_id = (await client.post("/sessions", json={})).json()["id"]
        first = asyncio.create_task(client.post(f"/sessions/{session_id}/messages/stream", json={"message": "a"}))
        await asyncio.sleep(0.05)

        second = await client.post(f"/sessions/{session_id}/messages/stream", json={"message": "b"})
        assert second.status_code == 409  # 推流开始前就明确拒绝，状态码是真实的 409

        release.set()
        assert (await first).status_code == 200


async def test_client_disconnect_releases_lock_and_rolls_back_history(store):
    entered = asyncio.Event()

    class Hangs(ScriptedLLM):
        async def generate_with_tools(self, messages, tools, system=None):
            entered.set()
            await asyncio.sleep(3600)  # 模型"一直在想"，直到被取消

    use_llm(Hangs([]))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session_id = (await client.post("/sessions", json={})).json()["id"]
        request = asyncio.create_task(client.post(f"/sessions/{session_id}/messages/stream", json={"message": "go"}))
        await entered.wait()  # 等到 agent 真的跑到了模型调用这一步

        session = store.get(session_id)
        assert session.lock.locked()
        assert len(session.messages) == 1  # 用户消息已经进了历史

        request.cancel()  # 模拟用户关闭网页
        with pytest.raises(asyncio.CancelledError):
            await request
        await asyncio.sleep(0.05)  # 给服务端的清理逻辑一点时间

    assert not session.lock.locked()  # 锁已释放，会话没有被锁死
    assert session.messages == []  # 半截历史已回滚


async def test_on_step_callback_receives_each_step_in_order():
    registry = build_investigation_tools(load_scenario("bug-1042"))
    seen = []

    async def on_step(record):
        seen.append(record.tool)

    llm = ScriptedLLM(investigation_script())
    await InvestigationAgent(llm, registry).run_turn([], "go", "BUG-1042", require_report=True, on_step=on_step)
    assert seen == ["get_ticket", "search_logs", SUBMIT_TOOL_NAME]
