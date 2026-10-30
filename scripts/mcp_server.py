"""以 stdio 方式运行 MCP Server：被 AI 应用（比如 Claude Code）当作子进程启动，通过 stdin/stdout 说 MCP 协议。

    .venv/bin/python scripts/mcp_server.py                      # 用模拟场景 bug-1042 的数据（工单、日志、发布、关键词版 runbook）
    .venv/bin/python scripts/mcp_server.py --knowledge          # 另外接上知识库：语义检索 runbook 和历史事故（需要数据库和入库）

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

from mcp.server.stdio import stdio_server  # noqa: E402

from app.agent.tools import build_investigation_tools  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import create_pool  # noqa: E402
from app.llm.factory import LLMNotConfigured  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
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

    pool = None
    knowledge = None
    if args.knowledge:
        settings = Settings()
        try:
            embedder = create_embedder(settings)
        except LLMNotConfigured as exc:
            print(f"cannot enable the knowledge base: {exc}", file=sys.stderr)
            return 2
        pool = create_pool(settings.database_url, settings.db_pool_max_size, settings.db_pool_timeout_seconds)
        await pool.open(wait=False)
        knowledge = await active_knowledge_base(KnowledgeBase(pool, embedder))
        if knowledge is None:
            print("the knowledge base is empty or unavailable (run scripts/ingest_knowledge.py)", file=sys.stderr)
            await pool.close()
            return 3

    try:
        server = build_mcp_server(build_investigation_tools(scenario, knowledge))
        logger.info("mcp server starting", extra={"scenario": args.scenario, "knowledge": knowledge is not None})
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
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
