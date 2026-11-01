"""以 stdio 方式运行 MCP Server：被 AI 应用（比如 Claude Code）当作子进程启动，通过 stdin/stdout 说 MCP 协议。

    .venv/bin/python scripts/mcp_server.py                      # 用模拟场景 bug-1042 的数据（工单、日志、发布、关键词版 runbook）
    .venv/bin/python scripts/mcp_server.py --knowledge          # 另外接上知识库：语义检索 runbook 和历史事故（需要数据库和入库）
    .venv/bin/python scripts/mcp_server.py --transport http     # 通过 HTTP 提供服务（默认只绑定本机 127.0.0.1:8765/mcp），需要 Bearer 密钥

【stdio 传输的规则】
这个进程的 stdout 是【协议通道】：它只能输出 MCP 的 JSON-RPC 消息，多一个字节都会让对方解析失败。
所以日志必须写到 stderr，调试时也不能随手 print()。
你不会在终端里直接看到有意义的输出：它在等对方通过 stdin 发请求。用它的方式是让别的程序来启动它。

接到 Claude Code：
    claude mcp add infra-debugging -- <项目路径>/.venv/bin/python <项目路径>/scripts/mcp_server.py
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# 直接运行脚本时，Python 的模块搜索路径里是脚本所在的目录（scripts/）而不是项目根目录。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402

from app.agent.common import AGENT_VARIANTS  # noqa: E402
from app.agent.tools import build_investigation_tools  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import create_pool, init_schema  # noqa: E402
from app.llm.factory import LLMNotConfigured  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.mcp_http import build_mcp_http_app  # noqa: E402
from app.mcp_server import build_mcp_server  # noqa: E402
from app.rag.embedder import create_embedder  # noqa: E402
from app.rag.retrieval import KnowledgeBase  # noqa: E402
from app.rag.runtime import active_knowledge_base  # noqa: E402
from app.scenario import load_scenario, scenario_exists  # noqa: E402

logger = logging.getLogger("mcp_server")


async def run(args: argparse.Namespace) -> int:
    configure_logging("INFO", stream=sys.stderr)  # 日志去 stderr，stdout 留给协议

    if not scenario_exists(args.scenario):
        print(f"unknown scenario {args.scenario!r}", file=sys.stderr)
        return 2
    scenario = load_scenario(args.scenario)

    settings = Settings()
    pool = None
    knowledge = None
    # HTTP 模式【总是】需要数据库（校验密钥）；stdio 模式只有开了知识库才需要。
    if args.knowledge or args.transport == "http":
        pool = create_pool(settings.database_url, settings.db_pool_max_size, settings.db_pool_timeout_seconds)
        await pool.open(wait=False)
    if args.knowledge:
        try:
            embedder = create_embedder(settings)
        except LLMNotConfigured as exc:
            print(f"cannot enable the knowledge base: {exc}", file=sys.stderr)
            return 2
        knowledge = await active_knowledge_base(KnowledgeBase(pool, embedder))
        if knowledge is None:
            print("the knowledge base is empty or unavailable (run scripts/ingest_knowledge.py)", file=sys.stderr)
            await pool.close()
            return 3

    try:
        discovery = AGENT_VARIANTS[args.agent_variant].service_discovery
        server = build_mcp_server(build_investigation_tools(scenario, knowledge, service_discovery=discovery))
        logger.info("mcp server starting", extra={"scenario": args.scenario, "knowledge": knowledge is not None})
        if args.transport == "http":
            try:
                await pool.open(wait=True, timeout=5)  # 起来之前确认数据库可用；不可用就不要对外提供服务
                await init_schema(pool)
                app = build_mcp_http_app(server, pool, host=args.host, allowed_hosts=args.allowed_host)
            except Exception as exc:
                print(f"cannot start the HTTP server: {exc}", file=sys.stderr)
                return 4
            config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
            await uvicorn.Server(config).serve()
        else:
            async with stdio_server() as (read_stream, write_stream):
                await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        if pool is not None:
            await pool.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the investigation tools as an MCP server over stdio.")
    parser.add_argument("--scenario", default="bug-1042", help="which simulated incident's data to serve")
    parser.add_argument("--knowledge", action="store_true", help="serve semantic knowledge search (needs the database)")
    parser.add_argument(
        "--agent-variant", choices=["v1", "v2"], default=Settings().agent_variant,
        help="v2 also exposes the list_services tool (default: the AGENT_VARIANT setting)",
    )
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="http only. Default is localhost; exposing it needs TLS in front")
    parser.add_argument("--port", type=int, default=8765, help="http only")
    parser.add_argument(
        "--allowed-host", action="append", default=[], help="http only: allowed Host header (required for non-local --host)"
    )
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
