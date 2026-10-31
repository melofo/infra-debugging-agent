"""批量运行评估：逐个案例运行、边跑边写结果、支持中断后续跑，最后打印报告。

【为什么要"边跑边写"和"续跑"】
真实模型跑 30 个案例要几十分钟（免费额度每分钟只有几次调用）。跑到一半网络断了、额度用完了、
或者 Ctrl-C，如果结果只在最后才写，前面的钱和时间就白费了。
所以每个案例做完立刻追加一行到 JSONL 文件；再次运行时读出已经完成的 (场景, 第几次)，只补做没做完的。
（和任务队列"幂等、可重试"是同一种思路。）
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.evals.agent_eval import CaseResult, result_from_dict, result_to_dict, run_case, summarize
from app.evals.agent_eval import key_service_info
from app.evals.scoring import correct_rank, text_is_correct
from app.llm.base import LLMClient
from app.scenario import load_scenario
from app.schemas import RCAReport

LlmFactory = Callable[[dict], LLMClient]  # 给定清单里的一项，返回这个案例用的模型客户端


def load_results(path: Path) -> list[CaseResult]:
    if not path.is_file():
        return []
    results = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            results.append(result_from_dict(json.loads(line)))
    return results


def rescore(results: list[CaseResult]) -> tuple[list[CaseResult], int]:
    """用【当前的】评分规则，对已经保存的结果重新打分（不调用模型，不花钱）。返回 (新结果, 其中只能近似打分的个数)。

    用途：评分规则有问题、改了之后，不用重跑几十分钟、花额度，也不会因为模型每次回答不同而把"评分规则的变化"
    和"模型的随机波动"混在一起。新的结果文件保存了完整报告，可以精确重打；
    老的结果文件只保存了排第一的假设的文字，只能近似重打 Top-1（Top-3 保持原样）。
    """
    approximate = 0
    rescored = []
    for old in results:
        new = CaseResult(**{**old.__dict__})
        if new.status == "ok":
            truth = load_scenario(new.scenario).ground_truth
            if new.report is not None:
                rank = correct_rank(RCAReport.model_validate(new.report), truth.keywords, truth.must_have)
                new.correct_rank, new.top1 = rank, rank == 1
                new.top3 = rank is not None and rank <= 3
            else:
                approximate += 1
                new.top1 = text_is_correct(new.top_cause, truth.keywords, truth.must_have)
                new.top3 = new.top3 or new.top1
            # "有没有去关键服务查日志"的定义也可能改过：用保存下来的"查过哪些服务"重新计算
            new.key_service_queried, new.cross_service = key_service_info(load_scenario(new.scenario), new.log_services)
        rescored.append(new)
    return rescored, approximate


def select_cases(
    manifest: list[dict], names: list[str] | None, category: str | None, difficulty: str | None, limit: int | None, split: str = "all"
) -> list[dict]:
    """split：dev = 开发集（调 agent 时用）、holdout = 留出集（调完之后才用来检验）、all = 全部。"""
    chosen = [
        e for e in manifest
        if (not names or e["scenario"] in names)
        and (split == "all" or e.get("split", "dev") == split)
        and (not category or e["category"] == category)
        and (not difficulty or e["difficulty"] == difficulty)
    ]
    return chosen[:limit] if limit else chosen


async def run_all(
    cases: list[dict],
    llm_factory: LlmFactory,
    out_path: Path,
    *,
    repeat: int = 1,
    sleep_seconds: float = 0.0,
    max_steps: int = 8,
    timeout_seconds: float = 180.0,
    variant: str = "v1",
    on_result: Callable[[CaseResult], Awaitable[None] | None] | None = None,
) -> list[CaseResult]:
    """返回【这次文件里所有的结果】（包括之前已经完成的）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = {(r.scenario, r.repeat) for r in load_results(out_path)}
    for entry in cases:
        for index in range(repeat):
            if (entry["scenario"], index) in done:
                continue
            result = await run_case(entry, llm_factory(entry), repeat=index, max_steps=max_steps, timeout_seconds=timeout_seconds, variant=variant)
            with out_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result_to_dict(result), ensure_ascii=False) + "\n")  # 做完一个存一个
            if on_result is not None:
                outcome = on_result(result)
                if asyncio.iscoroutine(outcome):
                    await outcome
            if sleep_seconds:
                await asyncio.sleep(sleep_seconds)
    wanted = {(e["scenario"], i) for e in cases for i in range(repeat)}
    return [r for r in load_results(out_path) if (r.scenario, r.repeat) in wanted]


def format_report(summary: dict, title: str) -> str:
    def pct(block: dict) -> str:
        if block["rate"] is None:
            return "n/a"
        low, high = block["ci95"]
        return f"{block['rate'] * 100:5.1f}%  ({block['count']}/{block['n']}, 95% CI {low * 100:.0f}-{high * 100:.0f}%)"

    lines = [f"=== {title} ===", f"cases: {summary['cases']}"]
    lines.append(f"Top-1 accuracy        {pct(summary['top1'])}")
    lines.append(f"Top-3 accuracy        {pct(summary['top3'])}")
    lines.append(f"errors (counted wrong) {pct(summary['errors'])}  {summary['error_kinds'] or ''}")
    lines.append(f"cites the key evidence {pct(summary['cites_culprit'])}")
    lines.append(f"report rejected at least once for unverifiable evidence: {pct(summary['had_grounding_rejection'])}")
    lines.append(f"cross-service cases: looked in the service holding the key evidence {pct(summary['looked_in_the_key_service_when_it_was_another_service'])}")
    lines.append(f"   accuracy when it looked there {pct(summary['accuracy_when_it_looked_in_the_key_service'])}")
    lines.append(f"   accuracy when it did not      {pct(summary['accuracy_when_it_did_not'])}")
    lines.append(f"total rejections: grounding {summary['total_grounding_rejections']}, invalid format {summary['total_schema_rejections']}")
    gap = summary["calibration_gap"]
    if gap is not None:
        lines.append(f"calibration gap       {gap:+.2f}  (mean confidence minus accuracy; positive = overconfident)")
    for heading, key in (("by difficulty", "by_difficulty"), ("by culprit deploy", "by_culprit_deploy"), ("by category", "by_category")):
        lines.append(f"-- {heading}")
        for name, stats in summary[key].items():
            lines.append(f"   {name:34} n={stats['n']:<3} top1={stats['top1'] * 100:5.1f}%  top3={stats['top3'] * 100:5.1f}%")
    if summary["unstable_cases"]:
        lines.append(f"unstable across repeats: {', '.join(summary['unstable_cases'])}")
    c = summary["cost"]
    lines.append(
        f"cost per case: {c['avg_llm_calls']} model calls, {c['avg_tool_calls']} tool calls, "
        f"{c['avg_input_tokens']} in / {c['avg_output_tokens']} out tokens; latency p50 {c['latency_ms_p50']}ms p95 {c['latency_ms_p95']}ms"
    )
    if c["estimated_cost_usd"] is not None:
        lines.append(f"estimated cost for this run: ${c['estimated_cost_usd']}")
    return "\n".join(lines)


def summarize_file(path: Path, **prices) -> dict:
    return summarize(load_results(path), **prices)
