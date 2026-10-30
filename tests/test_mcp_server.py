"""MCP Server 的测试。

两种连接方式都测：
  内存直连  Client(server)：不起进程、不走网络，最快，用来测协议行为
  stdio     Client(StdioServerParameters)：真的启动 scripts/mcp_server.py 子进程，像 Claude Code 那样连它，
            用来证明"作为独立程序运行"这条路径是通的（模块搜索路径、stdout 是否干净等只有这样才测得到）
"""

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from app.agent.tools import ToolRegistry, build_investigation_tools
from app.mcp_server import build_mcp_server
from app.scenario import load_scenario

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mcp_server.py"


@pytest.fixture
def registry() -> ToolRegistry:
    return build_investigation_tools(load_scenario("bug-1042"))


def connect(registry: ToolRegistry) -> Client:
    """内存直连的 MCP 客户端，用法：`async with connect(registry) as client:`。

    不做成 pytest 的异步 fixture：MCP 客户端内部用 anyio 的任务组，要求"在哪个任务里进入就在同一个任务里退出"，
    而 pytest-asyncio 的异步 fixture 的准备和清理可能跑在不同的任务里，会报
    "Attempted to exit cancel scope in a different task"。所以每个测试自己用 async with 打开和关闭。
    """
    return Client(build_mcp_server(registry))


# ------------------------------------------------------------ tools/list
async def test_tools_list_matches_the_tool_registry(registry):
    async with connect(registry) as client:
        result = await client.list_tools()

        assert [t.name for t in result.tools] == registry.names()
        for tool, spec in zip(result.tools, registry.specs()):
            assert tool.description == spec.description
            assert tool.input_schema == spec.parameters  # 一份定义同时服务 Agent 和 MCP，不会两边不一致


async def test_the_agents_terminal_tool_is_not_exposed(registry):
    async with connect(registry) as client:
        names = [t.name for t in (await client.list_tools()).tools]
        assert "submit_rca_report" not in names  # 它是 Agent 自己的动作，不是数据源能力


async def test_every_tool_is_declared_read_only(registry):
    async with connect(registry) as client:
        for tool in (await client.list_tools()).tools:
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.open_world_hint is False


async def test_input_schemas_are_real_json_schemas_a_client_can_validate_with(registry):
    async with connect(registry) as client:
        search_logs = next(t for t in (await client.list_tools()).tools if t.name == "search_logs")
        schema = search_logs.input_schema
        assert schema["type"] == "object" and schema["required"] == ["service"]
        assert schema["properties"]["limit"]["maximum"] == 50  # 范围限制也在 schema 里，客户端提前就知道


# ------------------------------------------------------------ tools/call
async def test_a_successful_call_returns_text_and_structured_content(registry):
    async with connect(registry) as client:
        result = await client.call_tool("get_ticket", {"ticket_id": "BUG-1042"})

        assert result.is_error is False
        assert result.structured_content["service"] == "checkout-api"
        assert json.loads(result.content[0].text) == result.structured_content  # 同一份数据的两种表示


async def test_list_results_are_wrapped_because_structured_content_must_be_an_object(registry):
    async with connect(registry) as client:
        result = await client.call_tool("get_recent_deploys", {"service": "checkout-api"})
        assert result.is_error is False
        assert result.structured_content["result"][0]["version"] == "2.14.0"  # 列表被包在 {"result": [...]} 里


async def test_tool_failures_are_reported_as_error_results_the_model_can_read(registry):
    async with connect(registry) as client:
        # 工具执行失败 = is_error 的【正常结果】，不是协议错误：模型看到原因后能自己改正再试。
        bad_ticket = await client.call_tool("get_ticket", {"ticket_id": "BUG-9999"})
        wrong_service = await client.call_tool("search_logs", {"service": "payment-service"})
        too_many = await client.call_tool("search_logs", {"service": "checkout-api", "limit": 500})

        assert bad_ticket.is_error and "ticket not found" in bad_ticket.content[0].text
        assert wrong_service.is_error and "known services" in wrong_service.content[0].text
        assert too_many.is_error  # 参数范围校验照样生效


async def test_unknown_tools_and_missing_arguments_are_rejected(registry):
    async with connect(registry) as client:
        assert (await client.call_tool("delete_database", {})).is_error  # 白名单：不存在的工具执行不了
        assert (await client.call_tool("get_ticket", {})).is_error  # 缺少必填参数


async def test_unexpected_crashes_do_not_leak_internal_details(registry):
    secret = "postgres://admin:hunter2@10.1.2.3/prod"

    async def boom(args):
        raise RuntimeError(f"cannot connect to {secret}")

    from app.agent.tools import GetTicketArgs, Tool

    crashing = ToolRegistry([Tool("get_ticket", "x", GetTicketArgs, boom)])
    async with Client(build_mcp_server(crashing)) as connected:
        result = await connected.call_tool("get_ticket", {"ticket_id": "BUG-1042"})

    assert result.is_error and result.content[0].text == "the tool failed unexpectedly"
    assert "hunter2" not in result.content[0].text and "10.1.2.3" not in result.content[0].text


async def test_the_ground_truth_never_reaches_mcp_clients(registry):
    async with connect(registry) as client:
        # 评估用的标准答案绝不能通过任何工具泄露出去（对 MCP 客户端同样如此）。
        scenario = load_scenario("bug-1042")
        outputs = [
            (await client.call_tool("get_ticket", {"ticket_id": "BUG-1042"})).content[0].text,
            (await client.call_tool("search_logs", {"service": "checkout-api"})).content[0].text,
            (await client.call_tool("get_recent_deploys", {"service": "checkout-api"})).content[0].text,
            (await client.call_tool("search_runbooks", {"query": "connection pool"})).content[0].text,
        ]
        assert scenario.ground_truth.root_cause not in "".join(outputs)


# ------------------------------------------------------------ 真正的 stdio 子进程
def stdio_params(tmp_path, *extra: str) -> StdioServerParameters:
    # cwd 设成无关的空目录、不设 PYTHONPATH：完全复现"被 Claude Code 之类的程序启动"的情形。
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "GEMINI_API_KEY")}
    return StdioServerParameters(command=sys.executable, args=[str(SCRIPT), *extra], cwd=tmp_path, env=env)


async def test_the_stdio_server_works_as_a_real_subprocess(tmp_path):
    async with Client(stdio_params(tmp_path)) as client:
        names = [t.name for t in (await client.list_tools()).tools]
        result = await client.call_tool("search_logs", {"service": "checkout-api", "level": "ERROR", "query": "hikari"})

    assert names == ["get_ticket", "search_logs", "get_recent_deploys", "search_runbooks"]
    assert result.structured_content["total_matches"] == 3  # 真实数据，经过真实的协议往返


async def test_an_unknown_scenario_fails_cleanly_instead_of_hanging(tmp_path):
    import subprocess

    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--scenario", "../../etc/passwd"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
    )
    assert done.returncode == 2 and "unknown scenario" in done.stderr
    assert done.stdout == ""  # 出错信息只去 stderr，stdout（协议通道）保持干净
