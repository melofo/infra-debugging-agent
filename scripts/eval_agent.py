"""端到端评估：让 agent 调查一批场景，统计找对根因的比例和代价。

    # 先跑不花钱的参照线，确认评估本身是通的（几秒钟）：
    .venv/bin/python scripts/eval_agent.py --llm oracle           # 上限：必须 100%
    .venv/bin/python scripts/eval_agent.py --llm latest_deploy    # "怪最近一次发布"：应该很低
    .venv/bin/python scripts/eval_agent.py --llm runbook_copy     # "照抄 runbook"：应该接近 0

    # 对照实验：同一套题，改动前后各跑一遍（--agent v1 是现在的配置，v2 是实验配置；--max-steps 单独控制步数上限）：
    .venv/bin/python scripts/eval_agent.py --llm gemini --agent v2 --max-steps 12 --out eval_results/v2.jsonl
    .venv/bin/python scripts/eval_agent.py --llm gemini --split holdout --agent v2 --max-steps 12   # 留出集：没调过的新题型

    # 真实模型（花额度！先用 --limit 3 试一下）：
    .venv/bin/python scripts/eval_agent.py --llm gemini --limit 3
    .venv/bin/python scripts/eval_agent.py --llm gemini --sleep 15 --out eval_results/gemini-run1.jsonl
    .venv/bin/python scripts/eval_agent.py --llm gemini --repeat 3                 # 每个案例跑 3 次，看稳定性
    .venv/bin/python scripts/eval_agent.py --llm gemini --difficulty hard --category expired_certificate

    # 改了评分规则之后，对已经跑完的结果重新打分（不花钱）：
    .venv/bin/python scripts/eval_agent.py --rescore eval_results/run1.jsonl

中断后用同一个 --out 再运行，会跳过已完成的案例继续（结果是边跑边写的）。
免费额度每分钟只有几次调用：用 --sleep 在案例之间等一会儿。--price-in / --price-out 是每百万 token 的美元单价，
填了就会估算费用（不填就不估算，避免用我记不准的价格误导你）。
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# 直接运行脚本时，Python 的模块搜索路径里是脚本所在的目录（scripts/）而不是项目根目录。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.evals.baselines import BASELINES  # noqa: E402
from app.evals.runner import format_report, load_results, rescore, run_all, select_cases, summarize  # noqa: E402
from app.llm.factory import LLMNotConfigured, create_llm_client  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.scenario import SCENARIO_DIR, load_scenario  # noqa: E402

MANIFEST_PATH = SCENARIO_DIR.parent / "eval" / "agent_scenarios.json"


async def run(args: argparse.Namespace) -> int:
    configure_logging("ERROR")  # 评估时不要刷屏；我们自己打印进度和报告
    if args.rescore:
        old = load_results(Path(args.rescore))
        if not old:
            print(f"no results found in {args.rescore}")
            return 2
        new, approximate = rescore(old)
        print(f"re-scoring {len(new)} saved results with the current scoring rule (no model calls)")
        if approximate:
            print(f"note: {approximate} results were saved without the full report, so only their top hypothesis text was re-scored")
        before, after = summarize(old)["top1"], summarize(new)["top1"]
        print(f"Top-1 before: {before['count']}/{before['n']}   after: {after['count']}/{after['n']}")
        changed = [o.scenario for o, n in zip(old, new) if o.top1 != n.top1]
        print(f"cases whose verdict changed: {', '.join(changed) or 'none'}\n")
        print(format_report(summarize(new, price_in_per_million=args.price_in, price_out_per_million=args.price_out), "re-scored"))
        return 0
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    cases = select_cases(manifest, args.scenario, args.category, args.difficulty, args.limit, args.split)
    if not cases:
        print("no cases match those filters")
        return 2

    if args.llm == "gemini":
        try:
            client = create_llm_client(Settings())
        except LLMNotConfigured as exc:
            print(f"Cannot run the real model: {exc}. Run scripts/check_env.py to see what is missing.")
            return 2
        factory = lambda entry: client  # noqa: E731
    else:
        baseline = BASELINES[args.llm]
        factory = lambda entry: baseline(load_scenario(entry["scenario"]))  # noqa: E731

    out = Path(args.out or f"eval_results/agent-{args.llm}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    total = len(cases) * args.repeat
    print(f"running {total} runs ({len(cases)} cases x {args.repeat}) with {args.llm}, agent {args.agent}, max steps {args.max_steps}; results are appended to {out}")

    counter = {"n": 0}

    def progress(result) -> None:
        counter["n"] += 1
        mark = "top1" if result.top1 else ("top3" if result.top3 else ("ERR " if result.status != "ok" else "miss"))
        print(f"  [{counter['n']}/{total}] {result.scenario} {result.category:28} {mark}  {result.latency_ms}ms  rej={result.grounding_rejections}", flush=True)

    results = await run_all(
        cases, factory, out, repeat=args.repeat, sleep_seconds=args.sleep,
        max_steps=args.max_steps, timeout_seconds=args.timeout, variant=args.agent, on_result=progress,
    )
    summary = summarize(results, price_in_per_million=args.price_in, price_out_per_million=args.price_out)
    print()
    print(format_report(summary, f"{args.llm}: {len(cases)} cases"))
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nsummary written to {args.json}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the agent on the incident scenarios.")
    parser.add_argument("--llm", choices=["gemini", *BASELINES], default="oracle")
    parser.add_argument("--scenario", action="append", help="only this scenario (repeatable), e.g. bug-2003")
    parser.add_argument("--split", choices=["dev", "holdout", "all"], default="dev", help="dev (default): the 30 scenarios used while developing; holdout: 6 unseen incident types, for checking an improvement generalises")
    parser.add_argument("--category")
    parser.add_argument("--difficulty", choices=["easy", "medium", "hard"])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--sleep", type=float, default=0.0, help="seconds to wait between runs (rate limits)")
    parser.add_argument("--agent", choices=["v1", "v2"], default="v1", help="agent configuration: v1 = current; v2 = experiment (list_services tool + 'beyond deploys' prompt)")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--out", help="JSONL file for per-case results (also used to resume)")
    parser.add_argument("--rescore", metavar="FILE", help="re-score a saved results file with the current scoring rule (free, no model calls)")
    parser.add_argument("--json", help="also write the summary as JSON to this file")
    parser.add_argument("--price-in", type=float, default=0.0, help="USD per million input tokens")
    parser.add_argument("--price-out", type=float, default=0.0, help="USD per million output tokens")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
