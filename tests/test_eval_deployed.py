"""scripts/eval_deployed.py 的测试：对"已部署的实例"做端到端评估。不联网：用假的 HTTP 服务代替部署的 API。

要证明的：每个场景都走"签名的 Webhook -> 轮询任务"；API 密钥只用在查任务上，Webhook 靠签名；
评分用的是同一个评分器（答对/答错都能分清）；各种失败（没入队、任务失败、读不到）都变成一条"答错"记录，
而不是让整批评估中断；可以中断后接着跑；密钥永远不会被打印。
"""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from app.scenario import load_scenario

_spec = importlib.util.spec_from_file_location("eval_deployed", Path(__file__).resolve().parents[1] / "scripts" / "eval_deployed.py")
eval_deployed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_deployed)

SECRET, KEY = "webhook-secret-value", "api-key-value"


def correct_report_for(scenario_name: str) -> dict:
    """一份能被评分器判对的报告：把标准答案里的关键事实和概念写进根因里。"""
    truth = load_scenario(scenario_name).ground_truth
    words = " ".join(group.split("|")[0] for group in [*truth.must_have, *truth.keywords])
    return {
        "incident_id": scenario_name.upper(),
        "summary": "s",
        "hypotheses": [{"root_cause": f"{truth.root_cause} {words}", "confidence": 0.9, "evidence": [{"source": "jira", "summary": "s", "reference": "x"}]}],
    }


WRONG_REPORT = {
    "incident_id": "X", "summary": "s",
    "hypotheses": [{"root_cause": "the moon is made of cheese", "confidence": 0.9, "evidence": [{"source": "jira", "summary": "s", "reference": "x"}]}],
}


class Deployed:
    """假的"已部署实例"：Webhook 返回 202 和任务号；查任务返回预设的结果。"""

    def __init__(self, report=None, queue_status=202, job_status="succeeded", job_http=200):
        self.report, self.queue_status, self.job_status, self.job_http = report, queue_status, job_status, job_http
        self.webhook_calls, self.job_calls, self.tickets = 0, [], {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.webhook_calls += 1
            assert request.headers["x-hub-signature"].startswith("sha256=")  # 签了名
            assert "authorization" not in request.headers  # API 密钥不发给 Webhook
            ticket = json.loads(request.content)["issue"]["key"]
            job_id = f"job-{ticket}"
            self.tickets[job_id] = ticket
            return httpx.Response(self.queue_status, json={"status": "queued", "job_id": job_id})
        self.job_calls.append(request.headers.get("authorization"))
        if self.job_http != 200:
            return httpx.Response(self.job_http, json={})
        ticket = self.tickets[request.url.path.rsplit("/", 1)[1]]
        report = self.report(ticket.lower()) if callable(self.report) else self.report
        result = {"report": report, "llm_calls": 6, "input_tokens": 1000, "output_tokens": 100} if self.job_status == "succeeded" else None
        return httpx.Response(200, json={"status": self.job_status, "attempts": 1, "max_attempts": 3, "result": result, "last_error": "boom"})


@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("AGENT_API_KEY", KEY)


def run(server, tmp_path, *extra, out_name="r.jsonl"):
    lines: list[str] = []
    client = httpx.Client(transport=httpx.MockTransport(server))
    code = eval_deployed.main(["--url", "http://api.test", "--limit", "3", "--out", str(tmp_path / out_name), *extra], client=client, out=lines.append)
    rows = [json.loads(line) for line in (tmp_path / out_name).read_text(encoding="utf-8").splitlines()] if (tmp_path / out_name).is_file() else []
    return code, "\n".join(lines), rows


def test_correct_reports_score_as_top1_and_the_job_poll_carries_the_api_key(tmp_path):
    server = Deployed(report=correct_report_for)
    code, output, rows = run(server, tmp_path)
    assert code == 0 and len(rows) == 3 and all(r["top1"] and r["status"] == "ok" for r in rows)
    assert server.webhook_calls == 3 and set(server.job_calls) == {f"Bearer {KEY}"}
    assert "Top-1 3/3" in output and "6.0 model calls, 1000 in / 100 out tokens" in output


def test_a_wrong_answer_is_a_miss_not_an_error(tmp_path):
    code, output, rows = run(Deployed(report=WRONG_REPORT), tmp_path)
    assert all(not r["top1"] and r["status"] == "ok" for r in rows) and "Top-1 0/3" in output and "errors 0" in output


@pytest.mark.parametrize(
    "kwargs, status",
    [(dict(queue_status=401, report=WRONG_REPORT), "not_queued"), (dict(job_status="failed", report=WRONG_REPORT), "job_failed"), (dict(job_http=401, report=WRONG_REPORT), "timeout_or_unreadable")],
)
def test_failures_become_wrong_rows_and_the_batch_continues(tmp_path, kwargs, status):
    code, output, rows = run(Deployed(**kwargs), tmp_path)
    assert code == 0 and len(rows) == 3 and all(r["status"] == status and not r["top1"] for r in rows)
    assert "errors 3" in output


def test_a_second_run_skips_scenarios_that_are_already_done(tmp_path):
    first = Deployed(report=correct_report_for)
    run(first, tmp_path)
    second = Deployed(report=correct_report_for)
    code, output, rows = run(second, tmp_path)
    assert second.webhook_calls == 0 and len(rows) == 3  # 没有重复发送，结果文件也没有重复的行


def test_missing_secrets_stop_the_run_before_any_network_call(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY")
    server = Deployed(report=WRONG_REPORT)
    code, output, rows = run(server, tmp_path)
    assert code == 2 and server.webhook_calls == 0 and "never printed" in output


def test_the_secrets_are_never_printed_or_saved(tmp_path):
    code, output, rows = run(Deployed(report=correct_report_for), tmp_path)
    saved = (tmp_path / "r.jsonl").read_text(encoding="utf-8")
    assert SECRET not in output + saved and KEY not in output + saved


def test_an_unreachable_backend_gives_a_clear_message(tmp_path):
    def refuse(request):
        raise httpx.ConnectError("refused")

    code, output, rows = run(refuse, tmp_path)
    assert code == 3 and "Cannot reach" in output


def test_the_summary_counts_and_percentiles():
    rows = [
        {"status": "ok", "top1": True, "top3": True, "llm_calls": 4, "input_tokens": 100, "output_tokens": 10, "wall_seconds": 10.0},
        {"status": "ok", "top1": False, "top3": True, "llm_calls": 8, "input_tokens": 300, "output_tokens": 30, "wall_seconds": 30.0},
        {"status": "job_failed", "top1": False, "wall_seconds": 50.0},
    ]
    s = eval_deployed.summarize(rows)
    assert (s["cases"], s["top1"], s["top3"], s["errors"]) == (3, 1, 2, 1)
    assert (s["avg_llm_calls"], s["avg_input_tokens"], s["avg_output_tokens"]) == (6.0, 200, 20)
    assert (s["seconds_p50"], s["seconds_p95"]) == (30.0, 50.0)


def correct_at_position(position: int):
    """正确的假设排在第 position 位（置信度从高到低），前面都是错的。"""

    def build(scenario_name: str) -> dict:
        right = correct_report_for(scenario_name)["hypotheses"][0]
        evidence = [{"source": "jira", "summary": "s", "reference": "x"}]
        hypotheses = [{"root_cause": f"wrong idea {i}", "confidence": 0.9 - 0.1 * i, "evidence": evidence} for i in range(position - 1)]
        hypotheses.append({**right, "confidence": 0.9 - 0.1 * (position - 1)})
        return {"incident_id": scenario_name.upper(), "summary": "s", "hypotheses": hypotheses}

    return build


@pytest.mark.parametrize("position, top1, top3", [(1, True, True), (2, False, True), (3, False, True), (4, False, False)])
def test_top1_and_top3_depend_on_where_the_correct_hypothesis_ranks(tmp_path, position, top1, top3):
    code, output, rows = run(Deployed(report=correct_at_position(position)), tmp_path)
    assert all(r["top1"] is top1 and r["top3"] is top3 for r in rows)
