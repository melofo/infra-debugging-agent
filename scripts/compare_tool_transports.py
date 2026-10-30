"""量一量三种工具来源的代价：直接调用 / MCP（内存） / MCP（子进程）。不调用大模型，不花钱。

    .venv/bin/python scripts/compare_tool_transports.py
    .venv/bin/python scripts/compare_tool_transports.py --calls 200 2>/dev/null   # 子进程的日志会打到 stderr，丢掉它输出更干净

每一层都是有代价的：
  建立连接的一次性开销（startup）：每次调查开始时要付一次
  每次工具调用的延迟（call）：调查中每次调用工具都要付
对比之下，一次大模型调用通常要几百毫秒到几秒。所以要把这些数字放在"一次调查的总耗时"里看，
而不是孤立地看"慢了多少倍"。
"""

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

# 直接运行脚本时，Python 的模块搜索路径里是脚本所在的目录（scripts/）而不是项目根目录。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.scenario import load_scenario  # noqa: E402
from app.tool_sources import open_tools  # noqa: E402

ARGS = {"service": "checkout-api", "level": "ERROR", "limit": 20}


async def measure(source: str, calls: int) -> tuple[float, list[float]]:
    settings = Settings(_env_file=None, tool_source=source)
    scenario = load_scenario("bug-1042")
    started = time.perf_counter()
    async with open_tools(settings, "bug-1042", scenario, None) as tools:
        startup = (time.perf_counter() - started) * 1000
        durations = []
        for _ in range(calls):
            t = time.perf_counter()
            await tools.execute("search_logs", ARGS)
            durations.append((time.perf_counter() - t) * 1000)
    return startup, durations


async def run(calls: int) -> None:
    rows = []
    for source in ("direct", "mcp-memory", "mcp-stdio"):
        await measure(source, 3)  # 预热：第一次导入模块、建立缓存的开销不算进结果
        startup, durations = await measure(source, calls)
        durations.sort()
        rows.append((source, startup, statistics.median(durations), durations[int(len(durations) * 0.95) - 1]))

    base = rows[0][2] or 1e-9
    print(f"{calls} calls of search_logs per source (milliseconds)\n")
    print(f"{'tool source':<12}{'startup':>10}{'call median':>14}{'call p95':>11}{'vs direct':>12}")
    for source, startup, median, p95 in rows:
        print(f"{source:<12}{startup:>10.1f}{median:>14.3f}{p95:>11.3f}{median / base:>11.0f}x")
    print(
        "\nFor scale: one model call takes hundreds of milliseconds to seconds, and a typical investigation makes "
        "about 3 to 8 tool calls plus 4 to 6 model calls."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare the cost of direct calls and MCP.")
    parser.add_argument("--calls", type=int, default=50)
    asyncio.run(run(parser.parse_args().calls))


if __name__ == "__main__":
    main()
