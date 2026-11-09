"""对一个【已部署】的实例做端到端评估：和本地评估用同一批场景、同一个评分器，但走真实的网络路径。

    签名的 Jira Webhook  ->  API  ->  数据库里的任务队列  ->  worker  ->  模型  ->  报告  ->  GET /jobs/{id}

用途：部署之后的"验收"。本地评估证明 agent 本身好不好；这个脚本证明【部署出来的整套系统】好不好：
认证、队列、worker、数据库、模型通道都真的连通，并且每个环节的延迟和 token 用量是多少。

运行（密钥从环境变量读取，不会打印）：
    JIRA_WEBHOOK_SECRET=... AGENT_API_KEY=... python scripts/eval_deployed.py --url https://你的API地址 --split holdout

结果每完成一个就写一行 JSONL（可以中断后接着跑，已完成的场景会跳过）。
注意：它评的是服务器上配置的那个 agent 版本和模型，不是本机的配置。
"""

import argparse
import importlib.util
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 按路径加载同目录的 simulate_jira.py（它不是一个可安装的包，不能写成 `import simulate_jira`）：
# 复用它构造事件、签名、发送、轮询的函数，不重复实现一遍。
_spec = importlib.util.spec_from_file_location("simulate_jira", Path(__file__).resolve().parent / "simulate_jira.py")
simulate_jira = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(simulate_jira)

from app.evals.runner import select_cases  # noqa: E402
from app.evals.scoring import correct_rank, percentile  # noqa: E402
from app.scenario import SCENARIO_DIR, load_scenario  # noqa: E402
from app.schemas import RCAReport  # noqa: E402

MANIFEST_PATH = SCENARIO_DIR.parent / "eval" / "agent_scenarios.json"


def run_case(client: httpx.Client, url: str, secret: str, api_key: str, entry: dict, timeout: float = 300.0) -> dict:
    """把一个场景当作"新建 Bug"事件发出去，等任务结束，用评分器打分。任何失败都变成一条"答错"的记录。"""
    scenario = load_scenario(entry["scenario"])
    ticket = entry["scenario"].upper()
    result = {"scenario": entry["scenario"], "category": entry["category"], "status": "ok", "top1": False, "error": ""}
    started = time.perf_counter()
    body = simulate_jira.build_event(ticket)
    response = simulate_jira.send(client, url, body, simulate_jira.sign(secret, body), f"eval-{uuid.uuid4()}")
    if response.status_code != 202:
        return {**result, "status": "not_queued", "error": f"webhook answered HTTP {response.status_code}"}
    job = simulate_jira.wait_for_job(
        client, url, response.json()["job_id"], lambda _line: None, timeout=timeout, headers={"Authorization": f"Bearer {api_key}"}
    )
    result["wall_seconds"] = round(time.perf_counter() - started, 1)
    if job is None:
        return {**result, "status": "timeout_or_unreadable", "error": "no finished job within the timeout (or the key was refused)"}
    if job["status"] != "succeeded" or not job.get("result"):
        return {**result, "status": "job_failed", "error": str(job.get("last_error") or "")[:200]}
    report = RCAReport.model_validate(job["result"]["report"])
    rank = correct_rank(report, scenario.ground_truth.keywords, scenario.ground_truth.must_have)
    return {
        **result,
        "top1": rank == 1,
        "top3": rank is not None and rank <= 3,
        "confidence": report.hypotheses[0].confidence,
        "llm_calls": job["result"]["llm_calls"],
        "input_tokens": job["result"]["input_tokens"],
        "output_tokens": job["result"]["output_tokens"],
    }


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    ok = [r for r in rows if r["status"] == "ok"]
    walls = [r["wall_seconds"] for r in rows if "wall_seconds" in r]
    return {
        "cases": n,
        "top1": sum(r["top1"] for r in rows),
        "top3": sum(bool(r.get("top3")) for r in rows),
        "errors": n - len(ok),
        "avg_llm_calls": round(sum(r["llm_calls"] for r in ok) / len(ok), 2) if ok else None,
        "avg_input_tokens": round(sum(r["input_tokens"] for r in ok) / len(ok)) if ok else None,
        "avg_output_tokens": round(sum(r["output_tokens"] for r in ok) / len(ok)) if ok else None,
        "seconds_p50": percentile(walls, 0.5) if walls else None,
        "seconds_p95": percentile(walls, 0.95) if walls else None,
    }


def main(argv: list[str] | None = None, client: httpx.Client | None = None, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description="Evaluate a deployed instance end to end.")
    parser.add_argument("--url", required=True, help="base URL of the deployed API")
    parser.add_argument("--split", choices=["dev", "holdout", "all"], default="all")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for each job")
    parser.add_argument("--out", help="JSONL file for per-case results (also used to resume)")
    args = parser.parse_args(argv)

    secret, api_key = os.environ.get("JIRA_WEBHOOK_SECRET", ""), os.environ.get("AGENT_API_KEY", "")
    if not secret or not api_key:
        out("Set JIRA_WEBHOOK_SECRET and AGENT_API_KEY in the environment (they are never printed).")
        return 2
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    cases = select_cases(manifest, None, None, None, args.limit, args.split)
    out_path = Path(args.out or f"eval_results/deployed-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = {json.loads(line)["scenario"] for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()} if out_path.is_file() else set()
    own_client = client is None
    client = client or httpx.Client(timeout=30)
    try:
        out(f"evaluating {len(cases)} scenarios against {args.url}; results are appended to {out_path}")
        for entry in cases:
            if entry["scenario"] in done:
                continue
            row = run_case(client, args.url, secret, api_key, entry, args.timeout)
            with out_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")  # 做完一个存一个
            mark = "top1" if row["top1"] else ("ERR " if row["status"] != "ok" else "miss")
            out(f"  {row['scenario']} {row['category']:28} {mark}  {row.get('wall_seconds', '-')}s")
    except httpx.TransportError as exc:
        out(f"Cannot reach {args.url} ({type(exc).__name__}).")
        return 3
    finally:
        if own_client:
            client.close()
    rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip() and json.loads(line)["scenario"] in {c["scenario"] for c in cases}]
    summary = summarize(rows)
    out(
        f"\nTop-1 {summary['top1']}/{summary['cases']}  Top-3 {summary['top3']}/{summary['cases']}  errors {summary['errors']}\n"
        f"per investigation: {summary['avg_llm_calls']} model calls, {summary['avg_input_tokens']} in / {summary['avg_output_tokens']} out tokens; "
        f"wall time p50 {summary['seconds_p50']}s p95 {summary['seconds_p95']}s (includes queueing)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
