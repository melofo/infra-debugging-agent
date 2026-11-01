"""会话持久化的测试（真实 PostgreSQL，没启动时自动跳过）。

重点是"出问题时会怎样"：重启、两个实例同时操作、租约过期、持有者崩溃、请求被取消、内容太大。
"""

import asyncio
import contextlib
import json
import uuid

import anyio
import httpx
import psycopg
import pytest
from google.genai import types as genai_types

from app.agent.tools import build_investigation_tools
from app.api.chat import get_llm_client
from app.llm.base import LLMError, Message, ToolCall, ToolResult
from app.llm.codec import message_from_json, message_to_json
from app.main import app
from app.scenario import load_scenario
from app.schemas import RCAReport
from app.sessions import (
    PostgresSessionStore,
    SessionConflict,
    SessionTooLarge,
    SessionUnavailable,
    Turn,
    make_registry_factory,
    release_shielded,
)
from tests.fakes import GET_TICKET, SUBMIT_TOOL_NAME, VALID_REPORT_ARGS, ScriptedLLM, calls, final_text


# ============================================================ 编解码（不需要数据库）
def gemini_content() -> genai_types.Content:
    return genai_types.Content(
        role="model",
        parts=[
            genai_types.Part(text="checking", thought_signature=b"\x00\x01\xfe\xffsig"),
            genai_types.Part(function_call=genai_types.FunctionCall(name="search_logs", args={"service": "a", "limit": 3})),
        ],
    )


def test_every_kind_of_message_survives_a_json_round_trip():
    original = [
        Message(role="user", text="Investigate ticket BUG-1042."),
        Message(
            role="model",
            text="",
            tool_calls=(ToolCall("search_logs", {"service": "checkout-api", "limit": 3}),),
            raw=gemini_content(),
        ),
        Message(
            role="tool",
            tool_results=(
                ToolResult("search_logs", {"entries": [{"message": "中文 and \"quotes\""}]}),
                ToolResult("get_ticket", "ticket not found", is_error=True),
            ),
        ),
    ]
    restored = [message_from_json(json.loads(json.dumps(message_to_json(m)))) for m in original]

    assert restored == original  # raw 不参与 == 比较，下面单独验证
    assert restored[1].raw == original[1].raw
    assert restored[1].raw.parts[0].thought_signature == b"\x00\x01\xfe\xffsig"  # 字节原样还原：模型要求原样带回
    assert restored[2].tool_results[1].is_error is True


def test_an_unknown_raw_type_is_refused_instead_of_silently_dropped():
    with pytest.raises(TypeError, match="cannot persist"):
        message_to_json(Message(role="model", text="x", raw=object()))
    with pytest.raises(ValueError, match="unknown raw"):
        message_from_json({"role": "model", "raw": {"type": "something.else", "data": {}}})


# ============================================================ 夹具
def make_store(pool, knowledge=None, **kwargs) -> PostgresSessionStore:
    async def get_knowledge():
        return knowledge

    return PostgresSessionStore(pool, make_registry_factory(get_knowledge), **kwargs)


@pytest.fixture
async def store(pool):
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE chat_sessions")
    return make_store(pool)


@pytest.fixture
def registry():
    return build_investigation_tools(load_scenario("bug-1042"))


def sample_report() -> RCAReport:
    return RCAReport(incident_id="BUG-1042", **VALID_REPORT_ARGS)


async def make_session(store, registry, owner="alice", **kwargs):
    return await store.create("bug-1042", "BUG-1042", registry, owner=owner, **kwargs)


async def finish_a_turn(store, session):
    """模拟一轮：占用 -> 往历史里加东西 -> 保存。"""
    assert await store.acquire(session)
    session.messages.append(Message(role="user", text="hello"))
    session.messages.append(Message(role="model", text="hi", raw=gemini_content()))
    session.turns.append(
        Turn(user_message="hello", answer="hi", report=sample_report(), steps=[], llm_calls=1, input_tokens=10, output_tokens=5, latency_ms=3)
    )
    session.report = sample_report()
    await store.save(session)
    await store.release(session)


# ============================================================ 存、取、重启
async def test_a_session_survives_a_restart(pool, store, registry):
    session = await make_session(store, registry, owner="alice")
    await finish_a_turn(store, session)

    after_restart = make_store(pool)  # 全新的存储对象，相当于服务重启后
    restored = await after_restart.get(session.id)

    assert (restored.owner, restored.scenario_name, restored.ticket_id) == ("alice", "bug-1042", "BUG-1042")
    assert [m.role for m in restored.messages] == ["user", "model"]
    assert restored.messages[1].raw == gemini_content()  # 厂商原始数据也回来了
    assert restored.turns[0].answer == "hi" and restored.total_input_tokens == 10
    assert restored.report.hypotheses[0].root_cause == sample_report().hypotheses[0].root_cause
    assert "get_ticket" in restored.registry.names()  # 工具按创建时的方式重建


async def test_unknown_id_is_none(store):
    assert await store.get("nope") is None


async def test_ids_are_unguessable_and_unique(store, registry):
    ids = {(await make_session(store, registry)).id for _ in range(20)}
    assert len(ids) == 20 and all(len(i) >= 20 for i in ids)


async def test_a_new_session_starts_empty(store, registry):
    created = await make_session(store, registry)
    restored = await store.get(created.id)
    assert (restored.messages, restored.turns, restored.report, restored.version) == ([], [], None, 0)


# ============================================================ 租约：同一时刻只处理一条消息
async def test_only_one_holder_at_a_time_and_release_frees_it(store, registry):
    created = await make_session(store, registry)
    a, b = await store.get(created.id), await store.get(created.id)

    assert await store.acquire(a) is True
    assert await store.is_busy(b) is True
    assert await store.acquire(b) is False  # 不排队，直接说"忙"
    await store.release(a)
    assert await store.is_busy(b) is False
    assert await store.acquire(b) is True


async def test_two_simultaneous_acquires_have_exactly_one_winner(store, registry):
    created = await make_session(store, registry)
    handles = [await store.get(created.id) for _ in range(5)]
    results = await asyncio.gather(*(store.acquire(h) for h in handles))
    assert results.count(True) == 1


async def test_acquiring_refreshes_the_stale_copy_to_the_latest_state(pool, store, registry):
    """A 读到的是旧状态；B 在这期间完成了一轮。A 之后再占用时拿到的必须是 B 写入之后的最新历史。"""
    created = await make_session(store, registry)
    stale = await store.get(created.id)
    other_instance = make_store(pool)
    await finish_a_turn(other_instance, await other_instance.get(created.id))

    assert stale.messages == []
    assert await store.acquire(stale)
    assert [m.role for m in stale.messages] == ["user", "model"] and stale.version == 1


async def test_a_crashed_holder_does_not_lock_the_session_forever(pool, registry):
    short = make_store(pool, lease_seconds=0.3)
    created = await make_session(short, registry)
    crashed = await short.get(created.id)
    assert await short.acquire(crashed)  # 取得租约后进程"崩溃"，再也没有释放

    waiting = await short.get(created.id)
    assert await short.acquire(waiting) is False
    await asyncio.sleep(0.5)
    assert await short.acquire(waiting) is True  # 租约到期，会话自动恢复可用


# ============================================================ 围栏与版本：不能覆盖别人的写入
async def test_a_holder_whose_lease_expired_cannot_overwrite_the_new_holders_work(pool, registry):
    short = make_store(pool, lease_seconds=0.3)
    created = await make_session(short, registry)
    slow = await short.get(created.id)
    assert await short.acquire(slow)
    await asyncio.sleep(0.5)  # slow 太慢，租约过期

    fast = await short.get(created.id)
    await finish_a_turn(short, fast)  # 接手的 fast 完成了一轮并保存

    slow.messages.append(Message(role="user", text="slow's late write"))
    with pytest.raises(SessionConflict):
        await short.save(slow)  # 迟到的写入被拒绝
    latest = await short.get(created.id)
    assert [m.text for m in latest.messages] == ["hello", "hi"]  # fast 的结果完好无损


async def test_releasing_an_expired_lease_does_not_clear_the_new_holders_lease(pool, registry):
    short = make_store(pool, lease_seconds=0.3)
    created = await make_session(short, registry)
    slow = await short.get(created.id)
    await short.acquire(slow)
    await asyncio.sleep(0.5)
    fast = await short.get(created.id)
    assert await short.acquire(fast)

    await short.release(slow)  # 过期的持有者"迟到地"释放

    assert await short.is_busy(fast) is True  # fast 的租约没有被清掉


async def test_saving_twice_under_one_lease_keeps_the_version_in_step(pool, store, registry):
    created = await make_session(store, registry)
    session = await store.get(created.id)
    assert await store.acquire(session)
    session.messages.append(Message(role="user", text="one"))
    await store.save(session)
    session.messages.append(Message(role="user", text="two"))
    await store.save(session)  # 第二次保存不能因为本地版本号落后而被误判为冲突
    assert (await store.get(created.id)).version == 2


async def test_saving_without_holding_the_lease_is_refused(store, registry):
    created = await make_session(store, registry)
    session = await store.get(created.id)
    session.messages.append(Message(role="user", text="sneaky"))
    with pytest.raises(SessionConflict):
        await store.save(session)


async def test_a_version_mismatch_is_refused(pool, store, registry):
    created = await make_session(store, registry)
    session = await store.get(created.id)
    assert await store.acquire(session)
    async with pool.connection() as conn:
        await conn.execute("UPDATE chat_sessions SET version = version + 5 WHERE id = %s", (created.id,))
    with pytest.raises(SessionConflict):
        await store.save(session)


# ============================================================ 资源上限
async def test_oversized_content_is_refused_and_the_stored_session_is_untouched(pool, registry):
    small = make_store(pool, max_bytes=2000)
    created = await make_session(small, registry)
    session = await small.get(created.id)
    assert await small.acquire(session)
    session.messages.append(Message(role="tool", tool_results=(ToolResult("search_logs", "x" * 5000),)))

    with pytest.raises(SessionTooLarge):
        await small.save(session)
    assert (await small.get(created.id)).messages == []


async def test_each_owner_keeps_at_most_the_configured_number_of_sessions(pool, registry):
    capped = make_store(pool, max_per_owner=2)
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE chat_sessions")
    bob = await make_session(capped, registry, owner="bob")
    alice = [await make_session(capped, registry, owner="alice") for _ in range(3)]

    assert await capped.get(alice[0].id) is None  # alice 最旧的被淘汰
    assert await capped.get(alice[1].id) and await capped.get(alice[2].id)
    assert await capped.get(bob.id) is not None  # 不影响别人的


async def test_expired_sessions_are_purged_and_recent_ones_kept(pool, store, registry):
    old, middle, recent = [await make_session(store, registry) for _ in range(3)]
    async with pool.connection() as conn:
        await conn.execute("UPDATE chat_sessions SET updated_at = now() - interval '40 days' WHERE id = %s", (old.id,))
        await conn.execute("UPDATE chat_sessions SET updated_at = now() - interval '10 days' WHERE id = %s", (middle.id,))

    assert await store.purge_expired(ttl_days=30) == 1
    assert await store.get(old.id) is None
    assert await store.get(middle.id) is not None  # 10 天前的，还没到 30 天的期限
    assert await store.get(recent.id) is not None


# ============================================================ 恢复时重建工具
class FakeKnowledge:
    pass


async def test_a_session_that_used_the_knowledge_base_is_rebuilt_with_it(pool, registry):
    with_kb = make_store(pool, knowledge=FakeKnowledge())
    created = await make_session(with_kb, build_investigation_tools(load_scenario("bug-1042"), FakeKnowledge()), uses_knowledge=True)
    assert "search_knowledge" in (await with_kb.get(created.id)).registry.names()


async def test_a_session_is_not_silently_continued_with_a_different_tool_set(pool, registry):
    created = await make_session(make_store(pool, knowledge=FakeKnowledge()), registry, uses_knowledge=True)
    without_kb = make_store(pool, knowledge=None)  # 重启之后知识库不可用
    with pytest.raises(SessionUnavailable):
        await without_kb.get(created.id)


async def test_a_session_whose_scenario_disappeared_is_unavailable(pool, store, registry):
    created = await make_session(store, registry)
    async with pool.connection() as conn:
        await conn.execute("UPDATE chat_sessions SET scenario = 'gone-scenario' WHERE id = %s", (created.id,))
    with pytest.raises(SessionUnavailable):
        await store.get(created.id)


# ============================================================ 请求被取消时也要释放租约
async def test_the_lease_is_released_even_when_the_request_is_being_cancelled(store, registry):
    """被取消的作用域里，之后的每个 await 都会立刻再次被取消；没有 shield 的话，释放根本发不出去。"""
    created = await make_session(store, registry)
    session = await store.get(created.id)
    assert await store.acquire(session)

    with anyio.CancelScope() as scope:
        try:
            scope.cancel()
            await anyio.sleep(1)  # 在已取消的作用域里等待：被取消
        finally:
            await release_shielded(store, session)

    assert await store.is_busy(await store.get(created.id)) is False


async def test_a_failing_release_is_logged_not_raised(registry):
    class Broken:
        async def release(self, session):
            raise psycopg.OperationalError("db down")

    await release_shielded(Broken(), object())  # 不能抛出：否则会盖掉原本真正的错误


# ============================================================ 通过 HTTP 接口使用
@pytest.fixture
async def api(pool, store):
    """应用用数据库存储，模型是假的。返回 (客户端, 换一个全新的存储对象的函数——模拟重启)。"""
    had, old = hasattr(app.state, "session_store"), getattr(app.state, "session_store", None)
    had_pool, old_pool = hasattr(app.state, "pool"), getattr(app.state, "pool", None)
    app.state.session_store = store
    app.state.pool = pool
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
    if had:
        app.state.session_store = old
    else:
        del app.state.session_store
    if had_pool:
        app.state.pool = old_pool
    else:
        del app.state.pool


def use_llm(llm):
    app.dependency_overrides[get_llm_client] = lambda: llm


def restart(pool, **kwargs):
    app.state.session_store = make_store(pool, **kwargs)


async def test_the_conversation_continues_after_a_restart(api, pool):
    use_llm(ScriptedLLM([calls(GET_TICKET, ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)), final_text("Done.")]))
    session_id = (await api.post("/sessions", json={})).json()["id"]
    first = await api.post(f"/sessions/{session_id}/messages", json={"message": "investigate"})
    assert first.status_code == 200

    restart(pool)  # 服务重启
    shown = (await api.get(f"/sessions/{session_id}")).json()
    assert len(shown["turns"]) == 1 and shown["turns"][0]["answer"] == "Done."  # 刷新页面能恢复界面

    follow_up_llm = ScriptedLLM([final_text("Because the pool shrank.")])
    use_llm(follow_up_llm)
    second = await api.post(f"/sessions/{session_id}/messages", json={"message": "why?"})
    assert second.json()["answer"] == "Because the pool shrank."
    # 追问时模型看到了重启之前的完整历史（包括第一轮的工具结果），所以这是追问而不是重新调查
    history = follow_up_llm.seen_messages[0]
    assert history[0].text == "investigate" and any(m.role == "tool" for m in history)
    assert second.status_code == 200 and len((await api.get(f"/sessions/{session_id}")).json()["turns"]) == 2


async def test_a_failed_turn_changes_nothing_and_frees_the_session(api, pool):
    class Fails(ScriptedLLM):
        async def generate_with_tools(self, messages, tools, system=None):
            raise LLMError("provider down")

    use_llm(Fails([]))
    session_id = (await api.post("/sessions", json={})).json()["id"]
    assert (await api.post(f"/sessions/{session_id}/messages", json={"message": "go"})).status_code == 502
    restored = await make_store(pool).get(session_id)
    assert restored.messages == [] and restored.turns == []  # 失败的轮次没有留下任何东西

    use_llm(ScriptedLLM([calls(GET_TICKET, ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)), final_text()]))
    assert (await api.post(f"/sessions/{session_id}/messages", json={"message": "retry"})).status_code == 200


async def test_a_second_message_while_one_is_running_gets_409_even_across_instances(api, pool):
    release = asyncio.Event()

    class Slow(ScriptedLLM):
        async def generate_with_tools(self, messages, tools, system=None):
            await release.wait()
            return await super().generate_with_tools(messages, tools, system)

    use_llm(Slow([calls(GET_TICKET, ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)), final_text()]))
    session_id = (await api.post("/sessions", json={})).json()["id"]
    first = asyncio.create_task(api.post(f"/sessions/{session_id}/messages", json={"message": "one"}))
    await asyncio.sleep(0.2)

    restart(pool)  # "另一个实例"（全新的存储对象，没有共享任何内存）也看得到这个会话正忙
    second = await api.post(f"/sessions/{session_id}/messages", json={"message": "two"})
    assert second.status_code == 409

    release.set()
    assert (await first).status_code == 200


async def test_cancelling_a_running_request_frees_the_session_and_leaves_history_clean(api, pool):
    entered = asyncio.Event()

    class Hangs(ScriptedLLM):
        async def generate_with_tools(self, messages, tools, system=None):
            entered.set()
            await asyncio.sleep(3600)

    use_llm(Hangs([]))
    session_id = (await api.post("/sessions", json={})).json()["id"]
    request = asyncio.create_task(api.post(f"/sessions/{session_id}/messages", json={"message": "go"}))
    await entered.wait()
    assert await app.state.session_store.is_busy(await app.state.session_store.get(session_id))

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await asyncio.sleep(0.2)

    store = make_store(pool)
    assert await store.is_busy(await store.get(session_id)) is False
    assert (await store.get(session_id)).messages == []


async def test_the_turn_limit_still_applies_after_a_restart(api, pool, monkeypatch):
    monkeypatch.setattr("app.api.sessions.get_settings", lambda: type("S", (), {"max_turns_per_session": 1, "agent_max_steps": 8, "agent_variant": "v1"})())
    use_llm(ScriptedLLM([calls(GET_TICKET, ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)), final_text()]))
    session_id = (await api.post("/sessions", json={})).json()["id"]
    assert (await api.post(f"/sessions/{session_id}/messages", json={"message": "a"})).status_code == 200
    restart(pool)
    assert (await api.post(f"/sessions/{session_id}/messages", json={"message": "b"})).status_code == 409


async def test_the_database_going_away_is_a_503_without_details(api, monkeypatch):
    async def boom(*args, **kwargs):
        raise psycopg.OperationalError("connection to server at 10.1.2.3 refused")

    monkeypatch.setattr(app.state.session_store, "get", boom)
    response = await api.get("/sessions/whatever")
    assert response.status_code == 503 and "10.1.2.3" not in response.text


async def test_a_conflicting_save_and_an_oversized_session_are_409s(api, monkeypatch):
    use_llm(ScriptedLLM([calls(GET_TICKET, ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)), final_text()] * 2))
    session_id = (await api.post("/sessions", json={})).json()["id"]
    for error, expected in ((SessionConflict("x"), "changed elsewhere"), (SessionTooLarge("x"), "too large")):
        async def raising(session, _e=error):
            raise _e

        monkeypatch.setattr(app.state.session_store, "save", raising)
        response = await api.post(f"/sessions/{session_id}/messages", json={"message": "go"})
        assert response.status_code == 409 and expected in response.json()["detail"]


async def test_an_unrestorable_session_is_a_503(api, pool):
    session_id = (await api.post("/sessions", json={})).json()["id"]
    async with pool.connection() as conn:
        await conn.execute("UPDATE chat_sessions SET uses_knowledge = true WHERE id = %s", (session_id,))
    assert (await api.get(f"/sessions/{session_id}")).status_code == 503  # 知识库不可用，不会悄悄换一套工具


async def test_the_owner_is_stored_so_ownership_checks_survive_a_restart(pool, store, registry):
    created = await make_session(store, registry, owner="alice")
    assert (await make_store(pool).get(created.id)).owner == "alice"
