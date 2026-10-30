"""根据配置决定一次调查的工具从哪里来：直接调用函数，还是通过 MCP。

    direct      Agent --> Python 函数                      最快，没有协议开销
    mcp-memory  Agent --> MCP 客户端 --(内存)--> MCP 服务端 --> Python 函数   同一进程，但走完整的 MCP 协议
    mcp-stdio   Agent --> MCP 客户端 --(管道)--> 子进程里的 MCP 服务端        真正的进程边界
    mcp-http    Agent --> MCP 客户端 --(HTTPS + Bearer 密钥)--> 远程 MCP 服务端   跨机器、需要认证

几种方式 Agent 看到的工具、拿到的结果完全一样（测试里逐个工具比对过）。
区别只在"调用要经过多少层"：层越多，越灵活（工具可以由别的团队、别的语言、别的机器提供），
也越慢、越多出错的可能。scripts/compare_tool_transports.py 可以量出每一层的代价。
"""

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.client.stdio import StdioServerParameters

from app.agent.tools import ToolProvider, build_investigation_tools
from app.config import Settings
from app.mcp_client import bearer_http_transport, connect_mcp
from app.mcp_server import build_mcp_server
from app.rag.retrieval import KnowledgeBase
from app.scenario import Scenario

MCP_SERVER_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mcp_server.py"

# 子进程需要知识库时，才把这几个变量传给它。最小权限：不把整个环境（含无关的密钥）都交给子进程。
KNOWLEDGE_ENV_VARS = ("DATABASE_URL", "GEMINI_API_KEY", "EMBEDDING_PROVIDER", "EMBEDDING_MODEL")


def stdio_parameters(scenario_name: str, with_knowledge: bool, environ: dict[str, str]) -> StdioServerParameters:
    args = [str(MCP_SERVER_SCRIPT), "--scenario", scenario_name]
    env: dict[str, str] = {}
    if with_knowledge:
        args.append("--knowledge")
        env = {name: environ[name] for name in KNOWLEDGE_ENV_VARS if name in environ}
    return StdioServerParameters(command=sys.executable, args=args, env=env)


@asynccontextmanager
async def open_tools(
    settings: Settings,
    scenario_name: str,
    scenario: Scenario,
    knowledge: KnowledgeBase | None,
    environ: dict[str, str] | None = None,
) -> AsyncIterator[ToolProvider]:
    if settings.tool_source == "direct":
        yield build_investigation_tools(scenario, knowledge)
        return

    if settings.tool_source == "mcp-memory":
        target = build_mcp_server(build_investigation_tools(scenario, knowledge))
    elif settings.tool_source == "mcp-http":
        # 远程服务端自己决定数据（场景、知识库）；这里只带上凭证。
        target = bearer_http_transport(settings.mcp_http_url, settings.mcp_api_key.get_secret_value())
    else:  # mcp-stdio
        import os

        target = stdio_parameters(scenario_name, knowledge is not None, environ if environ is not None else dict(os.environ))

    async with connect_mcp(target, call_timeout=settings.mcp_call_timeout_seconds) as tools:
        yield tools
