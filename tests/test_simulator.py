"""Jira 模拟器的测试。

最重要的是：确认"模拟器发出的请求，真实的 Webhook 接口确实按预期处理"。
模拟器和服务端是分开写的两份代码，如果各改各的，演示时才发现对不上就晚了。
所以这里让模拟器直接对真实的应用发请求（用 TestClient 当 HTTP 客户端），逐个场景检查。
"""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import jobs
from app.config import Settings
from app.db import get_pool
from app.main import app

# scripts/ 不是 Python 包（没有 __init__.py），所以按文件路径把它加载成模块。
_spec = importlib.util.spec_from_file_location(
    "simulate_jira", Path(__file__).resolve().parents[1] / "scripts" / "simulate_jira.py"
)
simulate_jira = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(simulate_jira)

SECRET = "simulator-test-secret"


@pytest.fixture
def client(pool, monkeypatch):
    configured = Settings(_env_file=None, jira_webhook_secret=SECRET)
    monkeypatch.setattr("app.api.webhooks.get_settings", lambda: configured)
    app.dependency_overrides[get_pool] = lambda: pool
    yield TestClient(app)
    app.dependency_overrides.clear()


def run(client, *argv: str):
    lines: list[str] = []
    code = simulate_jira.main(
        [*argv, "--url", "http://testserver"], client=client, secret=SECRET, out=lines.append
    )
    return code, "\n".join(lines)


@pytest.mark.parametrize(
    "kind, expected_jobs",
    [
        ("created", 1),
        ("duplicate", 1),  # 连发两次，只产生一个任务
        ("bad-signature", 0),
        ("tampered", 0),
        ("no-signature", 0),
        ("wrong-method", 0),
        ("non-bug", 0),
        ("updated", 0),
        ("unknown-ticket", 0),
    ],
)
async def test_every_scenario_gets_the_response_the_simulator_promises(client, pool, kind, expected_jobs):
    code, output = run(client, kind)

    assert code == 0, output  # 退出码 0 = 每个请求的状态码都和预期一致
    assert "UNEXPECTED" not in output
    assert len(await jobs.list_jobs(pool)) == expected_jobs


async def test_the_queued_job_is_for_the_requested_ticket(client, pool):
    run(client, "created", "--ticket", "BUG-1042")
    [job] = await jobs.list_jobs(pool)
    assert (job.ticket_id, job.scenario, job.status) == ("BUG-1042", "bug-1042", "queued")


async def test_reusing_a_delivery_id_is_deduplicated_across_runs(client, pool):
    run(client, "created", "--delivery-id", "fixed-id")
    code, output = run(client, "created", "--delivery-id", "fixed-id")
    # 第二次服务端返回 200（重复），而模拟器的 created 场景预期 202，所以会标记为意外：这正说明去重生效了。
    assert code == 1 and "HTTP 200" in output and "duplicate" in output
    assert len(await jobs.list_jobs(pool)) == 1


def test_the_simulator_reports_a_failure_exit_code_when_the_server_disagrees(pool, monkeypatch):
    # 服务端配了另一把密钥：模拟器的"正确签名"就会被拒绝，退出码应为 1，而不是假装成功。
    other = Settings(_env_file=None, jira_webhook_secret="a-different-secret")
    monkeypatch.setattr("app.api.webhooks.get_settings", lambda: other)
    app.dependency_overrides[get_pool] = lambda: pool
    try:
        code, output = run(TestClient(app), "created")
    finally:
        app.dependency_overrides.clear()
    assert code == 1 and "UNEXPECTED" in output


def test_without_a_secret_the_simulator_refuses_to_run(monkeypatch):
    monkeypatch.setattr("app.config.Settings", lambda: Settings(_env_file=None, jira_webhook_secret=""))
    lines: list[str] = []
    code = simulate_jira.main(["created"], client=None, secret=None, out=lines.append)
    assert code == 2 and "JIRA_WEBHOOK_SECRET is not set" in "\n".join(lines)


def test_the_secret_is_never_printed(client):
    _, output = run(client, "created")
    assert SECRET not in output
    assert "sha256=" not in output  # 签名本身也不打印


def test_wait_polls_until_the_job_finishes_and_prints_the_result(monkeypatch):
    # 不经过真实服务端：用假的 HTTP 响应序列验证 --wait 的轮询逻辑。任务状态依次是 queued -> running -> succeeded。
    states = iter(["queued", "running", "succeeded"])
    result = {
        "report": {"hypotheses": [{"confidence": 0.9, "root_cause": "pool size reduced"}]},
        "llm_calls": 3,
        "input_tokens": 1000,
        "output_tokens": 50,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"status": "queued", "job_id": "abc12345-0000"})
        status = next(states)
        job = {"status": status, "attempts": 1, "max_attempts": 3, "result": result if status == "succeeded" else None}
        return httpx.Response(200, json=job)

    # 轮询间隔设为 0，测试不用真的睡眠。
    original = simulate_jira.wait_for_job
    monkeypatch.setattr(simulate_jira, "wait_for_job", lambda c, u, j, o: original(c, u, j, o, interval=0))
    lines: list[str] = []
    fake = httpx.Client(transport=httpx.MockTransport(handler))  # MockTransport：不联网，直接返回我们指定的响应

    code = simulate_jira.main(["created", "--wait", "--url", "http://x"], client=fake, secret=SECRET, out=lines.append)

    output = "\n".join(lines)
    assert code == 0
    assert "is now running" in output and "is now succeeded" in output
    assert "pool size reduced" in output and "90%" in output


def test_the_events_it_builds_look_like_jira_payloads():
    body = json.loads(simulate_jira.build_event("BUG-1042"))
    assert body["webhookEvent"] == "jira:issue_created"
    assert body["issue"]["key"] == "BUG-1042"
    assert body["issue"]["fields"]["issuetype"]["name"] == "Bug"
