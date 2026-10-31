"""会话与追问的测试。

核心要验证的事：
  1. 追问时，模型确实能看到之前的工具结果（对话历史被正确延续）
  2. 失败的一轮不会污染历史（回滚）
  3. 并发、轮数上限、会话数量上限这些护栏有效
"""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent.loop import InvestigationAgent
from app.agent.tools import build_investigation_tools
from app.api.chat import get_llm_client
from app.config import Settings
from app.llm.base import LLMError, LLMResponse
from app.main import app
from app.scenario import load_scenario
from app.sessions import SessionStore, get_session_store
from tests.fakes import GET_TICKET, SUBMIT_TOOL_NAME, VALID_REPORT_ARGS, ScriptedLLM, ToolCall, calls, final_text


@pytest.fixture
def store():
    # 每个测试用一个全新的会话存储，互不影响。
    fresh = SessionStore(max_sessions=100)
    app.dependency_overrides[get_session_store] = lambda: fresh
    yield fresh  # yield 之前是准备，之后是清理（类似 JUnit 的 @BeforeEach + @AfterEach 合在一起）
    app.dependency_overrides.clear()


def use_llm(llm):
    app.dependency_overrides[get_llm_client] = lambda: llm


def investigation_script():
    """首轮调查的剧本：查工单 -> 提交报告 -> 模型文字收尾。"""
    return [
        calls(ToolCall("get_ticket", {"ticket_id": "BUG-1042"})),
        calls(ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)),
        final_text("The pool size change in 7be41d0 is the likely cause."),
    ]


def test_create_session(store):
    response = TestClient(app).post("/sessions", json={"scenario": "bug-1042"})
    assert response.status_code == 201
    body = response.json()
    assert body["ticket_id"] == "BUG-1042"
    assert body["turns"] == []
    assert len(body["id"]) >= 16  # 不可猜的随机 id


def test_create_session_rejects_bad_and_unknown_scenarios(store):
    client = TestClient(app)
    assert client.post("/sessions", json={"scenario": "../../.env"}).status_code == 422
    assert client.post("/sessions", json={"scenario": "nope"}).status_code == 404


def test_unknown_session_returns_404(store):
    use_llm(ScriptedLLM([]))
    client = TestClient(app)
    assert client.get("/sessions/does-not-exist").status_code == 404
    assert client.post("/sessions/does-not-exist/messages", json={"message": "hi"}).status_code == 404


def test_first_message_investigates_then_follow_up_reuses_history(store):
    llm = ScriptedLLM(investigation_script() + [calls(text="It was committed by kpatel in commit 7be41d0.")])
    use_llm(llm)
    client = TestClient(app)
    session_id = client.post("/sessions", json={}).json()["id"]

    first = client.post(f"/sessions/{session_id}/messages", json={"message": "Investigate this"}).json()
    assert first["report"] is not None
    assert [s["tool"] for s in first["steps"]] == ["get_ticket", SUBMIT_TOOL_NAME]
    # 首次调查的 answer 是模型对用户原话的文字回应，而不是报告摘要。
    assert first["answer"] == "The pool size change in 7be41d0 is the likely cause."
    assert first["llm_calls"] == 3

    follow_up = client.post(f"/sessions/{session_id}/messages", json={"message": "Who made the change?"}).json()
    # 追问：只有文字回答，没有新报告，也没有再调用工具。
    assert follow_up["answer"] == "It was committed by kpatel in commit 7be41d0."
    assert follow_up["report"] is None
    assert follow_up["steps"] == []

    # 关键断言：追问时发给模型的历史里，包含首轮的工具结果。
    history_seen_on_follow_up = llm.seen_messages[-1]
    roles = [m.role for m in history_seen_on_follow_up]
    assert roles == ["user", "model", "tool", "model", "tool", "model", "user"]
    first_tool_result = history_seen_on_follow_up[2].tool_results[0].result
    assert first_tool_result["service"] == "checkout-api"
    assert history_seen_on_follow_up[-1].text == "Who made the change?"


def test_system_prompt_names_the_sessions_ticket(store):
    class SpyLLM(ScriptedLLM):
        def __init__(self, responses):
            super().__init__(responses)
            # 放在 __init__ 里（实例属性）。如果写成类属性 `systems: list = []`，
            # 所有实例会共享同一个列表，就像 Java 里的 static 可变字段，是常见的坑。
            self.systems = []

        async def generate_with_tools(self, messages, tools, system=None):
            self.systems.append(system)
            return await super().generate_with_tools(messages, tools, system)

    llm = SpyLLM(investigation_script())
    use_llm(llm)
    client = TestClient(app)
    session_id = client.post("/sessions", json={}).json()["id"]
    client.post(f"/sessions/{session_id}/messages", json={"message": "go"})
    assert "ticket BUG-1042" in llm.systems[0]


def test_get_session_returns_transcript(store):
    use_llm(ScriptedLLM(investigation_script()))
    client = TestClient(app)
    session_id = client.post("/sessions", json={}).json()["id"]
    client.post(f"/sessions/{session_id}/messages", json={"message": "go"})

    body = client.get(f"/sessions/{session_id}").json()
    assert len(body["turns"]) == 1
    assert body["turns"][0]["user_message"] == "go"
    assert body["total_input_tokens"] == 30  # 三次模型调用，每次 10


def test_failed_follow_up_is_rolled_back_and_session_stays_usable(store):
    class FlakyLLM(ScriptedLLM):
        fail_next = False

        async def generate_with_tools(self, messages, tools, system=None):
            if self.fail_next:
                self.fail_next = False
                raise LLMError("provider down")
            return await super().generate_with_tools(messages, tools, system)

    llm = FlakyLLM(investigation_script() + [calls(text="recovered answer")])
    use_llm(llm)
    client = TestClient(app)
    session_id = client.post("/sessions", json={}).json()["id"]
    client.post(f"/sessions/{session_id}/messages", json={"message": "go"})

    session = store.peek(session_id)
    history_len_before = len(session.messages)

    llm.fail_next = True
    failed = client.post(f"/sessions/{session_id}/messages", json={"message": "follow-up"})
    assert failed.status_code == 502
    # 回滚：失败的这一轮没有在历史里留下任何痕迹（连用户那句话也撤销了）。
    assert len(session.messages) == history_len_before
    assert len(session.turns) == 1

    ok = client.post(f"/sessions/{session_id}/messages", json={"message": "follow-up"})
    assert ok.status_code == 200
    assert ok.json()["answer"] == "recovered answer"


def test_turn_limit_is_enforced(store, monkeypatch):
    limited = Settings(_env_file=None, max_turns_per_session=1)
    monkeypatch.setattr("app.api.sessions.get_settings", lambda: limited)
    use_llm(ScriptedLLM(investigation_script()))
    client = TestClient(app)
    session_id = client.post("/sessions", json={}).json()["id"]

    assert client.post(f"/sessions/{session_id}/messages", json={"message": "go"}).status_code == 200
    second = client.post(f"/sessions/{session_id}/messages", json={"message": "again"})
    assert second.status_code == 409


async def test_oldest_session_is_evicted_when_store_is_full():
    small = SessionStore(max_sessions=2)
    registry = build_investigation_tools(load_scenario("bug-1042"))
    first = await small.create("bug-1042", "BUG-1042", registry)
    second = await small.create("bug-1042", "BUG-1042", registry)
    third = await small.create("bug-1042", "BUG-1042", registry)

    assert await small.get(first.id) is None  # 最旧的被淘汰，内存占用有上限
    assert await small.get(second.id) is not None
    assert await small.get(third.id) is not None


async def test_concurrent_messages_to_one_session_get_409(store):
    # 并发测试需要在异步环境里同时发两个请求，所以用 httpx 的异步客户端，
    # 并通过 ASGITransport 直接调用应用（和 TestClient 的原理一样，只是异步的）。
    release = asyncio.Event()

    class SlowLLM(ScriptedLLM):
        async def generate_with_tools(self, messages, tools, system=None):
            await release.wait()  # 卡住，直到测试放行，模拟"模型还在思考"
            return await super().generate_with_tools(messages, tools, system)

    use_llm(SlowLLM([calls(GET_TICKET, ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)), final_text()]))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session_id = (await client.post("/sessions", json={})).json()["id"]
        first = asyncio.create_task(client.post(f"/sessions/{session_id}/messages", json={"message": "one"}))
        await asyncio.sleep(0.05)  # 让第一个请求先进去并持有锁

        second = await client.post(f"/sessions/{session_id}/messages", json={"message": "two"})
        assert second.status_code == 409

        release.set()
        assert (await first).status_code == 200


# ---- 直接测试循环本身的历史处理（不经过 HTTP） ----


def assert_history_is_valid(messages):
    """历史必须满足模型厂商的要求：每个带函数调用的 model 消息，紧跟一条等量结果的 tool 消息。"""
    for i, message in enumerate(messages):
        if message.role == "model" and message.tool_calls:
            following = messages[i + 1]
            assert following.role == "tool"
            assert len(following.tool_results) == len(message.tool_calls)


async def test_history_stays_valid_when_report_is_submitted_with_other_calls():
    registry = build_investigation_tools(load_scenario("bug-1042"))
    llm = ScriptedLLM(
        [
            calls(
                GET_TICKET,  # 同一批次里先查到的数据，可以被紧随其后的报告引用
                ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS),
                ToolCall("get_ticket", {"ticket_id": "BUG-1042"}),  # 同一圈里报告之后的只读调用照常执行
                ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS),  # 重复提交
            ),
            final_text("done"),
        ]
    )
    messages = []
    result = await InvestigationAgent(llm, registry).run_turn(messages, "go", "BUG-1042", require_report=True)

    assert result.report is not None
    assert result.answer == "done"
    assert_history_is_valid(messages)
    assert messages[-1].role == "model"  # 以模型的文字结尾，保证下一条用户消息能正确衔接
    first_ticket, accepted, ticket, duplicate = messages[-2].tool_results
    assert not first_ticket.is_error and not accepted.is_error and not ticket.is_error
    assert duplicate.is_error and "skipped" in duplicate.result  # 重复提交只认第一份，但历史里仍有对应结果


async def test_report_is_kept_when_final_text_call_fails():
    registry = build_investigation_tools(load_scenario("bug-1042"))

    class FailsOnFinalCall(ScriptedLLM):
        async def generate_with_tools(self, messages, tools, system=None):
            if not self._responses:
                raise LLMError("provider down")
            return await super().generate_with_tools(messages, tools, system)

    llm = FailsOnFinalCall([calls(GET_TICKET, ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS))])
    messages = []
    result = await InvestigationAgent(llm, registry).run_turn(messages, "go", "BUG-1042", require_report=True)

    # 报告已经提交成功，只是最后的文字收尾失败：不能因此丢掉整份报告。
    assert result.report is not None
    assert result.answer == result.report.summary  # 用摘要兜底
    assert_history_is_valid(messages)
    assert messages[-1].role == "model"


async def test_report_is_kept_when_submitted_on_the_last_allowed_step():
    registry = build_investigation_tools(load_scenario("bug-1042"))
    llm = ScriptedLLM([calls(GET_TICKET, ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS))])
    messages = []
    # max_steps=1：提交报告用掉了唯一的一圈，没有余量再让模型文字收尾。
    result = await InvestigationAgent(llm, registry, max_steps=1).run_turn(
        messages, "go", "BUG-1042", require_report=True
    )

    assert result.report is not None
    assert_history_is_valid(messages)
    assert messages[-1].role == "model"


async def test_failed_turn_leaves_history_untouched():
    registry = build_investigation_tools(load_scenario("bug-1042"))

    class DiesMidTurn(ScriptedLLM):
        async def generate_with_tools(self, messages, tools, system=None):
            if len(self.seen_messages) == 1:
                raise LLMError("network lost")
            return await super().generate_with_tools(messages, tools, system)

    llm = DiesMidTurn([calls(ToolCall("get_ticket", {"ticket_id": "BUG-1042"}))])
    messages = []
    with pytest.raises(LLMError):
        await InvestigationAgent(llm, registry).run_turn(messages, "go", "BUG-1042", require_report=True)

    # 第一圈已经把"用户消息 + 模型的工具请求 + 工具结果"追加进去了，失败后必须全部撤销。
    assert messages == []
