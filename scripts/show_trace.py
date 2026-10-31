"""跑一次调查，把它的追踪（span 树）和耗时打印出来：一次调查的时间到底花在哪？

    .venv/bin/python scripts/show_trace.py                       # 用不花钱的替身（oracle）演示树的结构
    .venv/bin/python scripts/show_trace.py --llm gemini --scenario bug-2010    # 真实模型：看每次模型调用、每次工具调用各用了多久

输出示例（缩进 = 父子关系，括号里是该 span 自己的耗时）：
    agent.turn  4821ms  steps=4 llm_calls=5 tokens=15000/480
      llm.generate  1204ms  in=2100 out=60 tool_calls=1
      tool.execute  1ms  get_ticket ok
      ...
这些 span 和线上导出到 Cloud Trace / Jaeger 的是同一份埋点；这里用内存导出器，只是为了在终端里直接看。
只显示元数据（工具名、token、状态），不显示提示词和日志内容。
"""

import argparse
import asyncio
import sys
from pathlib import Path

# 直接运行脚本时，Python 的模块搜索路径里是脚本所在的目录（scripts/）而不是项目根目录。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402

from app import telemetry  # noqa: E402
from app.agent.loop import InvestigationAgent  # noqa: E402
from app.agent.tools import build_investigation_tools  # noqa: E402
from app.config import Settings  # noqa: E402
from app.evals.baselines import Oracle  # noqa: E402
from app.llm.factory import LLMNotConfigured, create_llm_client  # noqa: E402
from app.scenario import load_scenario, scenario_exists  # noqa: E402


def describe(span) -> str:
    a = span.attributes or {}
    if span.name == "llm.generate":
        return f"in={a.get('llm.input_tokens')} out={a.get('llm.output_tokens')} tool_calls={a.get('llm.tool_calls')}"
    if span.name == "tool.execute":
        return f"{a.get('tool.name')} {'ok' if a.get('tool.ok') else 'FAILED'}"
    if span.name == "agent.turn":
        return f"steps={a.get('agent.steps')} llm_calls={a.get('agent.llm_calls')} tokens={a.get('agent.input_tokens')}/{a.get('agent.output_tokens')}"
    return " ".join(f"{k}={v}" for k, v in a.items())


def render(spans) -> list[str]:
    children: dict = {}
    for s in spans:
        children.setdefault(s.parent.span_id if s.parent else None, []).append(s)
    lines: list[str] = []

    def walk(parent_id, depth: int) -> None:
        for s in sorted(children.get(parent_id, []), key=lambda x: x.start_time):
            ms = (s.end_time - s.start_time) / 1e6
            flag = "  ERROR" if s.status.status_code == StatusCode.ERROR else ""
            events = "".join(f"  [{e.name}: {dict(e.attributes or {}).get('kind', '')}]" for e in s.events if e.name == "report.rejected")
            lines.append(f"{'  ' * depth}{s.name}  {ms:.0f}ms  {describe(s)}{flag}{events}")
            walk(s.context.span_id, depth + 1)

    walk(None, 0)
    return lines


async def run(args: argparse.Namespace) -> int:
    if not scenario_exists(args.scenario):
        print(f"unknown scenario {args.scenario!r}")
        return 2
    scenario = load_scenario(args.scenario)
    if args.llm == "gemini":
        try:
            llm = create_llm_client(Settings())
        except LLMNotConfigured as exc:
            print(f"Cannot run the real model: {exc}")
            return 2
    else:
        llm = Oracle(scenario)

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry.use_providers(provider, None)
    try:
        agent = InvestigationAgent(llm, build_investigation_tools(scenario), max_steps=args.max_steps)
        try:
            await agent.investigate(scenario.ticket.id)
        except Exception as exc:  # 失败的调查同样有追踪：这正是追踪最有用的时候
            print(f"investigation failed: {type(exc).__name__}")
    finally:
        telemetry.use_providers(None, None)
    print("\n".join(render(exporter.get_finished_spans())))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one investigation and print its trace.")
    parser.add_argument("--scenario", default="bug-1042")
    parser.add_argument("--llm", choices=["oracle", "gemini"], default="oracle")
    parser.add_argument("--max-steps", type=int, default=8)
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
