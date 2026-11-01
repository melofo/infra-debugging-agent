"""v2 成为默认 agent 配置之后的接线测试：每一个构造 agent 的入口，都必须用同一套配置。

依据评估（见 README "Evaluation-driven improvement"）：v2 = list_services 工具 + "不要只看发布"的提示词。
"评估里测的配置"和"线上用的配置"必须是同一份代码，否则评估数字对线上没有意义：这里逐个入口核对。
"""

import json

import httpx
import psycopg
import pytest

from app import jobs
from app.agent.common import AGENT_VARIANTS, SYSTEM_PROMPT, SYSTEM_PROMPT_V2, session_system_prompt
from app.api.chat import get_llm_client
from app.config import Settings
from app.evals import agent_eval
from app.main import app
from app.mcp_client import connect_mcp
from app.scenario import load_scenario
from app.sessions import PostgresSessionStore, SessionStore, make_registry_factory
from app.tool_sources import open_tools, stdio_parameters
from app.worker import Worker
from tests.fakes import GET_TICKET, SUBMIT_TOOL_NAME, VALID_REPORT_ARGS, ScriptedLLM, ToolCall, calls, final_text


class Recorder(ScriptedLLM):
    """记录模型每次被给了哪些工具、什么系统提示词。"""

    def __init__(self, responses):
        super().__init__(responses)
        self.seen: list[tuple[list[str], str | None]] = []

    async def generate_with_tools(self, messages, tools, system=None):
        self.seen.append(([t.name for t in tools], system))
        return await super().generate_with_tools(messages, tools, system)


def good_script():
    return [calls(GET_TICKET, ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)), final_text()]


# ============================================================ 默认值和单一来源
def test_v2_is_the_default_and_the_step_budget_matches_what_was_evaluated():
    assert Settings.model_fields["agent_variant"].default == "v2"
    assert Settings.model_fields["agent_max_steps"].default == 12


def test_the_variant_definitions_are_shared_between_production_and_the_evaluation():
    assert agent_eval.AGENT_VARIANTS is AGENT_VARIANTS  # 评估和线上读的是同一个对象，不会各改各的
    assert AGENT_VARIANTS["v1"].prompt == SYSTEM_PROMPT and not AGENT_VARIANTS["v1"].service_discovery
    assert AGENT_VARIANTS["v2"].prompt == SYSTEM_PROMPT_V2 and AGENT_VARIANTS["v2"].service_discovery


def test_an_unknown_variant_is_rejected_at_startup():
    with pytest.raises(Exception):
        Settings(_env_file=None, agent_variant="v3")


# ============================================================ 工具来源
async def names_for(settings: Settings, scenario_name="bug-2010"):
    scenario = load_scenario(scenario_name)
    async with open_tools(settings, scenario_name, scenario, None) as tools:
        return tools.names()


@pytest.mark.parametrize("source", ["direct", "mcp-memory", "mcp-stdio"])
async def test_every_tool_source_offers_list_services_only_under_v2(source):
    assert "list_services" in await names_for(Settings(_env_file=None, tool_source=source, agent_variant="v2"))
    assert "list_services" not in await names_for(Settings(_env_file=None, tool_source=source, agent_variant="v1"))


def test_the_stdio_subprocess_is_told_which_variant_to_serve():
    params = stdio_parameters("bug-1042", False, {}, "v2")
    assert params.args[-2:] == ["--agent-variant", "v2"]
    assert stdio_parameters("bug-1042", False, {}).args[-2:] == ["--agent-variant", "v1"]


# ============================================================ 各个入口
async def test_the_background_worker_uses_the_configured_variant(pool):
    for variant, prompt, has_tool in (("v2", SYSTEM_PROMPT_V2, True), ("v1", SYSTEM_PROMPT, False)):
        async with pool.connection() as conn:
            await conn.execute("TRUNCATE investigation_jobs CASCADE")
        await jobs.enqueue(pool, f"evt-variant-{variant}", "BUG-1042", "bug-1042")
        llm = Recorder(good_script())
        await Worker(pool, llm, Settings(_env_file=None, agent_variant=variant), worker_id="w").run_once()
        tool_names, system = llm.seen[0]
        assert system == prompt and ("list_services" in tool_names) is has_tool, variant


async def test_the_investigate_endpoint_uses_the_configured_variant(monkeypatch):
    from fastapi.testclient import TestClient

    for variant, prompt in (("v2", SYSTEM_PROMPT_V2), ("v1", SYSTEM_PROMPT)):
        monkeypatch.setattr("app.api.investigate.get_settings", lambda v=variant: Settings(_env_file=None, agent_variant=v))
        llm = Recorder(good_script())
        app.dependency_overrides[get_llm_client] = lambda: llm
        try:
            assert TestClient(app).post("/investigate", json={}).status_code == 200
        finally:
            app.dependency_overrides.clear()
        tool_names, system = llm.seen[0]
        assert system == prompt and ("list_services" in tool_names) is (variant == "v2")


def test_the_session_prompt_follows_the_variant_and_names_the_ticket():
    v2, v1 = session_system_prompt("BUG-1", "v2"), session_system_prompt("BUG-1", "v1")
    assert v2.startswith(SYSTEM_PROMPT_V2) and v1.startswith(SYSTEM_PROMPT) and not v1.startswith(SYSTEM_PROMPT_V2)
    assert v2.endswith("This conversation is about ticket BUG-1.")


async def session_client(store, llm, monkeypatch, variant):
    from app.api import sessions as sessions_api

    monkeypatch.setattr(sessions_api, "get_settings", lambda: Settings(_env_file=None, agent_variant=variant))
    app.state.session_store = store
    app.dependency_overrides[get_llm_client] = lambda: llm
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_a_new_chat_session_gets_the_configured_variant_and_keeps_it(monkeypatch):
    store = SessionStore()
    llm = Recorder(good_script())
    client = await session_client(store, llm, monkeypatch, "v2")
    try:
        async with client:
            session_id = (await client.post("/sessions", json={})).json()["id"]
            await client.post(f"/sessions/{session_id}/messages", json={"message": "go"})
    finally:
        app.dependency_overrides.clear()
    assert store.peek(session_id).agent_variant == "v2"
    tool_names, system = llm.seen[0]
    assert "list_services" in tool_names and system.startswith(SYSTEM_PROMPT_V2) and "ticket BUG-1042" in system


async def test_a_session_keeps_its_original_tool_set_even_after_the_setting_changes(pool, monkeypatch):
    """创建时是 v1 的会话，之后配置改成 v2：它的工具集不能中途变化（反过来 v2 会话换回 v1 会丢掉历史里用过的工具）。"""
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE chat_sessions")

    async def no_knowledge():
        return None

    store = PostgresSessionStore(pool, make_registry_factory(no_knowledge))
    client = await session_client(store, Recorder(good_script()), monkeypatch, "v1")
    try:
        async with client:
            session_id = (await client.post("/sessions", json={})).json()["id"]
    finally:
        app.dependency_overrides.clear()

    restored = await PostgresSessionStore(pool, make_registry_factory(no_knowledge)).get(session_id)  # 模拟重启之后
    assert restored.agent_variant == "v1" and "list_services" not in restored.registry.names()

    llm = Recorder(good_script())
    client = await session_client(store, llm, monkeypatch, "v2")  # 配置现在是 v2
    try:
        async with client:
            assert (await client.post(f"/sessions/{session_id}/messages", json={"message": "go"})).status_code == 200
    finally:
        app.dependency_overrides.clear()
    tool_names, system = llm.seen[0]
    assert "list_services" not in tool_names and system.startswith(SYSTEM_PROMPT) and not system.startswith(SYSTEM_PROMPT_V2)


async def test_old_sessions_without_a_recorded_variant_are_treated_as_v1_after_the_migration(pool):
    """旧表没有 agent_variant 这一列：启动时的建表脚本要给它补上，并且旧会话默认是 v1（它们创建时确实是 v1 的工具集）。"""
    from app.db import init_schema

    async with pool.connection() as conn:
        await conn.execute("TRUNCATE chat_sessions")
        await conn.execute("INSERT INTO chat_sessions (id, owner, scenario, ticket_id) VALUES ('old-session', 'alice', 'bug-1042', 'BUG-1042')")
        await conn.execute("ALTER TABLE chat_sessions DROP COLUMN agent_variant")  # 变回"升级之前"的旧表结构
    await init_schema(pool)  # 应用启动时做的事

    async def no_knowledge():
        return None

    restored = await PostgresSessionStore(pool, make_registry_factory(no_knowledge)).get("old-session")
    assert restored.agent_variant == "v1" and "list_services" not in restored.registry.names()


# ============================================================ MCP 服务端脚本
async def test_the_mcp_server_script_exposes_list_services_when_asked(tmp_path):
    from mcp.client.stdio import StdioServerParameters
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "mcp_server.py"
    for variant, expected in (("v2", True), ("v1", False)):
        params = StdioServerParameters(command=sys.executable, args=[str(script), "--agent-variant", variant], env={})
        async with connect_mcp(params) as tools:
            assert ("list_services" in tools.names()) is expected, variant


async def test_a_v2_session_keeps_list_services_after_a_restart_and_after_the_setting_goes_back_to_v1(pool, monkeypatch):
    """反方向同样要成立：历史里已经调用过 list_services 的会话，不能因为配置切回 v1 就丢掉这个工具。"""
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE chat_sessions")

    async def no_knowledge():
        return None

    store = PostgresSessionStore(pool, make_registry_factory(no_knowledge))
    client = await session_client(store, Recorder(good_script()), monkeypatch, "v2")
    try:
        async with client:
            session_id = (await client.post("/sessions", json={})).json()["id"]
    finally:
        app.dependency_overrides.clear()

    restored = await PostgresSessionStore(pool, make_registry_factory(no_knowledge)).get(session_id)  # 重启之后
    assert restored.agent_variant == "v2" and "list_services" in restored.registry.names()

    llm = Recorder(good_script())
    client = await session_client(store, llm, monkeypatch, "v1")  # 配置现在切回了 v1
    try:
        async with client:
            assert (await client.post(f"/sessions/{session_id}/messages", json={"message": "go"})).status_code == 200
    finally:
        app.dependency_overrides.clear()
    tool_names, system = llm.seen[0]
    assert "list_services" in tool_names and system.startswith(SYSTEM_PROMPT_V2)
