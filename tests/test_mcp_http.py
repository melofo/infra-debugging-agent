"""MCP over Streamable HTTP + Bearer 认证的测试（真实 PostgreSQL，没启动时自动跳过）。

三层验证：
  1. 进程内：真实的 MCP 客户端 --ASGI--> 真实的服务端应用（含认证中间件和真实数据库里的密钥）
  2. 配置与安全开关：不安全的配置要在启动时被拒绝
  3. 真实网络：用命令行脚本起一个真正的 HTTP 服务进程，再通过 open_tools 连它
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import pytest
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from app import auth
from app.agent.tools import build_investigation_tools
from app.config import Settings, ensure_safe_mcp_endpoint
from app.mcp_client import ToolSourceError, bearer_http_transport, connect_mcp
from app.mcp_http import build_mcp_http_app
from app.mcp_server import build_mcp_server
from app.scenario import load_scenario
from app.tool_sources import open_tools

BASE = "http://127.0.0.1:8765"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mcp_server.py"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def make_app(pool, **kwargs):
    server = build_mcp_server(build_investigation_tools(load_scenario("bug-1042")))
    return build_mcp_http_app(server, pool, **kwargs)


@pytest.fixture
async def keys(pool):
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE api_keys")
    return pool


async def key_for(pool, name, role="operator", **kw) -> str:
    return (await auth.create_key(pool, name, role, **kw))[0]


@asynccontextmanager
async def raw_client(app, headers=None):
    """直接发原始 HTTP 请求，用来检查状态码；服务端应用的生命周期（会话管理器）要手动进入。"""
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=BASE, headers=headers or {}
        ) as http:
            yield http


@asynccontextmanager
async def inproc_transport(app, key):
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=BASE, headers={"Authorization": f"Bearer {key}"}
        ) as http:
            async with streamable_http_client(f"{BASE}/mcp", http_client=http) as streams:
                yield streams


# ------------------------------------------------------------ 认证与授权
async def test_no_key_is_401_for_every_path_and_method(keys):
    async with raw_client(make_app(keys)) as http:
        for method, path in (("POST", "/mcp"), ("GET", "/mcp"), ("DELETE", "/mcp"), ("GET", "/nothing-here")):
            response = await http.request(method, path, headers=MCP_HEADERS)
            assert response.status_code == 401, (method, path)
            assert response.headers["www-authenticate"] == "Bearer"


async def test_all_credential_failures_look_identical(keys):
    revoked = await key_for(keys, "r")
    await auth.revoke_key(keys, auth.parse_key(revoked)[0])
    expired = await key_for(keys, "e", expires_in_days=-1)
    valid = await key_for(keys, "v")
    candidates = [{}, {"Authorization": "Bearer garbage"}, {"Authorization": "Bearer agk_deadbeef.x"}]
    candidates += [{"Authorization": f"Bearer {k}"} for k in (revoked, expired, valid[:-3] + "xxx")]

    async with raw_client(make_app(keys)) as http:
        responses = [await http.post("/mcp", json=INIT, headers={**MCP_HEADERS, **h}) for h in candidates]

    assert {r.status_code for r in responses} == {401}
    assert len({r.text for r in responses}) == 1


async def test_key_in_query_string_is_rejected(keys):
    key = await key_for(keys, "q")
    async with raw_client(make_app(keys)) as http:
        response = await http.post(f"/mcp?api_key={key}", json=INIT, headers=MCP_HEADERS)
    assert response.status_code == 401


async def test_viewer_is_403_and_operator_gets_through(keys):
    viewer = await key_for(keys, "v", "viewer")
    operator = await key_for(keys, "o", "operator")
    async with raw_client(make_app(keys)) as http:
        denied = await http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {viewer}"})
        allowed = await http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {operator}"})
    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["result"]["serverInfo"]["name"] == "infra-debugging-tools"


async def test_database_failure_fails_closed(keys, monkeypatch):
    import psycopg

    async def boom(pool, presented):
        raise psycopg.OperationalError("db down")

    monkeypatch.setattr(auth, "authenticate_key", boom)
    async with raw_client(make_app(keys)) as http:
        response = await http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": "Bearer agk_aa.bb"})
    assert response.status_code == 503


async def test_revoking_a_key_stops_it_immediately(keys):
    key = await key_for(keys, "k")
    app = make_app(keys)
    async with raw_client(app, {"Authorization": f"Bearer {key}", **MCP_HEADERS}) as http:
        assert (await http.post("/mcp", json=INIT)).status_code == 200
        await auth.revoke_key(keys, auth.parse_key(key)[0])
        assert (await http.post("/mcp", json=INIT)).status_code == 401


# ------------------------------------------------------------ 传输层防护
async def test_oversized_body_is_rejected(keys):
    key = await key_for(keys, "big")
    async with raw_client(make_app(keys), {"Authorization": f"Bearer {key}", **MCP_HEADERS}) as http:
        response = await http.post("/mcp", content=b"x" * (70 * 1024))
    assert response.status_code == 413


async def test_dns_rebinding_host_is_rejected_even_with_a_valid_key(keys):
    key = await key_for(keys, "rebind")
    headers = {"Authorization": f"Bearer {key}", **MCP_HEADERS}
    async with raw_client(make_app(keys), headers) as http:
        good = await http.post("/mcp", json=INIT)
        evil = await http.post("/mcp", json=INIT, headers={"Host": "evil.example.com"})
    assert good.status_code == 200
    assert evil.status_code in (400, 403, 421)


def test_refuses_to_build_without_a_pool():
    with pytest.raises(ValueError, match="database pool"):
        make_app(None)


async def test_non_local_bind_requires_allowed_hosts(keys):
    with pytest.raises(ValueError, match="allowed hosts"):
        make_app(keys, host="0.0.0.0")
    make_app(keys, host="0.0.0.0", allowed_hosts=["mcp.example.com"])  # 显式给出就可以


# ------------------------------------------------------------ 真实的 MCP 客户端 <-> 服务端
async def test_real_client_gets_the_same_tools_and_results_as_direct(keys):
    key = await key_for(keys, "agent")
    direct = build_investigation_tools(load_scenario("bug-1042"))
    async with connect_mcp(inproc_transport(make_app(keys), key)) as tools:
        assert sorted(tools.names()) == sorted(n for n in direct.names() if n != "submit_rca_report")
        via_mcp = await tools.execute("get_ticket", {"ticket_id": "BUG-1042"})
        assert via_mcp == await direct.execute("get_ticket", {"ticket_id": "BUG-1042"})


async def test_client_with_a_bad_key_fails_with_a_generic_tool_source_error(keys):
    with pytest.raises(ToolSourceError, match="tool server unavailable"):
        async with connect_mcp(inproc_transport(make_app(keys), "agk_deadbeef.nope")):
            pass


# ------------------------------------------------------------ 配置安全
def test_endpoint_safety_rules():
    for ok in ("https://mcp.example.com/mcp", "http://127.0.0.1:8765/mcp", "http://localhost:9/mcp"):
        ensure_safe_mcp_endpoint(ok)
    for bad in ("http://mcp.example.com/mcp", "http://10.0.0.5/mcp", "ftp://x/mcp", "http://127.0.0.1.evil.com/mcp"):
        with pytest.raises(ValueError):
            ensure_safe_mcp_endpoint(bad)


def test_settings_reject_unsafe_mcp_http_configs():
    with pytest.raises(ValidationError, match="MCP_API_KEY"):
        Settings(_env_file=None, tool_source="mcp-http")
    with pytest.raises(ValidationError, match="https"):
        Settings(_env_file=None, tool_source="mcp-http", mcp_api_key="agk_a.b", mcp_http_url="http://example.com/mcp")
    Settings(_env_file=None, tool_source="mcp-http", mcp_api_key="agk_a.b", mcp_http_url="https://example.com/mcp")


def test_api_key_is_masked_when_settings_are_printed():
    settings = Settings(_env_file=None, mcp_api_key="agk_abcd1234.SUPER-SECRET-VALUE")
    assert "SUPER-SECRET-VALUE" not in repr(settings) + str(settings)


async def test_transport_refuses_to_send_a_key_over_plain_http():
    with pytest.raises(ValueError, match="https"):
        async with bearer_http_transport("http://mcp.example.com/mcp", "agk_a.b"):
            pass


# ------------------------------------------------------------ 真实网络：脚本起一个 HTTP 服务进程
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_for_port(port: int, process: subprocess.Popen, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"server exited early: {process.stderr.read()[-500:]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            await asyncio.sleep(0.2)
    raise AssertionError("server did not start")


async def test_real_http_server_process_end_to_end(keys, tmp_path):
    operator = await key_for(keys, "agent-op")
    viewer = await key_for(keys, "agent-view", "viewer")
    port = free_port()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["DATABASE_URL"] = keys.conninfo
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--transport", "http", "--port", str(port)],
        cwd=tmp_path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        await wait_for_port(port, process)
        url = f"http://127.0.0.1:{port}/mcp"

        def settings_for(key):
            return Settings(_env_file=None, tool_source="mcp-http", mcp_http_url=url, mcp_api_key=key)

        scenario = load_scenario("bug-1042")
        direct = build_investigation_tools(scenario)
        async with open_tools(settings_for(operator), "bug-1042", scenario, None) as tools:
            assert await tools.execute("get_ticket", {"ticket_id": "BUG-1042"}) == await direct.execute("get_ticket", {"ticket_id": "BUG-1042"})
            logs = await tools.execute("search_logs", {"service": "checkout-api", "level": "ERROR", "limit": 5})
            assert logs == await direct.execute("search_logs", {"service": "checkout-api", "level": "ERROR", "limit": 5})

        for bad in (viewer, "agk_deadbeef.wrong"):
            with pytest.raises(ToolSourceError):
                async with open_tools(settings_for(bad), "bug-1042", scenario, None):
                    pass

        async with httpx2.AsyncClient() as http:  # 不带密钥直接打真实端口
            assert (await http.post(url, json=INIT, headers=MCP_HEADERS)).status_code == 401
    finally:
        process.terminate()
        process.wait(timeout=10)
        process.stderr.close()


def test_script_refuses_to_serve_http_on_a_public_address_without_allowed_hosts(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--transport", "http", "--host", "0.0.0.0", "--port", str(free_port())],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 4
    assert "allowed hosts" in result.stderr


async def test_server_is_stateless_so_any_instance_can_serve_any_request(keys):
    """没有 initialize、没有会话 id，单独一个请求也能直接调用工具：说明服务端不保存会话状态，
    所以起多个实例（比如 Cloud Run）时不需要"同一个客户端必须回到同一个实例"。"""
    key = await key_for(keys, "stateless")
    request = {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}
    async with raw_client(make_app(keys), {"Authorization": f"Bearer {key}", **MCP_HEADERS}) as http:
        response = await http.post("/mcp", json=request)
    assert response.status_code == 200
    assert "mcp-session-id" not in response.headers
    assert "get_ticket" in {t["name"] for t in response.json()["result"]["tools"]}
