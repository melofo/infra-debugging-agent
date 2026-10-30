"""GET /jobs 和 GET /jobs/{id} 的测试（真实 PostgreSQL，没启动时自动跳过）。

成功的任务用【真实的 Worker + 假模型】产出，而不是手工拼一份 JSON：
这样同时验证了"Worker 写进去的格式"和"API 读出来的格式"是一致的。
手写 JSON 的话，两边各自改了结构也发现不了。
"""

import pytest
from fastapi.testclient import TestClient

from app import jobs
from app.config import Settings
from app.db import get_pool
from app.main import app
from app.worker import Worker
from tests.fakes import AutoLLM


@pytest.fixture
def client(pool):
    app.dependency_overrides[get_pool] = lambda: pool
    yield TestClient(app)
    app.dependency_overrides.clear()


async def make_succeeded_job(pool, event_id="evt-ok"):
    await jobs.enqueue(pool, event_id, "BUG-1042", "bug-1042")
    await Worker(pool, AutoLLM(), Settings(_env_file=None), worker_id="w1").run_once()
    return next(j for j in await jobs.list_jobs(pool) if j.event_id == event_id)


async def make_failed_job(pool, event_id="evt-bad"):
    await jobs.enqueue(pool, event_id, "BUG-9999", "does-not-exist")  # 没有对应数据 -> 永久失败
    await Worker(pool, AutoLLM(), Settings(_env_file=None), worker_id="w1").run_once()
    return next(j for j in await jobs.list_jobs(pool) if j.event_id == event_id)


# ------------------------------------------------------------ 列表
async def test_list_is_empty_when_there_are_no_jobs(client):
    assert client.get("/jobs").json() == []


async def test_list_shows_a_preview_of_each_job_without_the_heavy_result(client, pool):
    await make_succeeded_job(pool)

    [item] = client.get("/jobs").json()

    assert item["status"] == "succeeded"
    assert item["ticket_id"] == "BUG-1042"
    assert item["top_cause"].startswith("maximumPoolSize")  # 排名第一的根因，用于列表预览
    assert item["confidence"] == 0.9
    # 列表接口不返回完整结果：每行带一整份报告会很大，详情页才需要。
    assert "result" not in item and "steps" not in item


async def test_list_shows_failed_and_queued_jobs_too(client, pool):
    await make_failed_job(pool)
    await jobs.enqueue(pool, "evt-waiting", "BUG-1042", "bug-1042")  # 还没人处理

    by_status = {item["status"]: item for item in client.get("/jobs").json()}

    assert by_status["failed"]["top_cause"] is None
    assert "invalid job" in by_status["failed"]["last_error"]
    assert by_status["queued"]["attempts"] == 0


async def test_list_is_newest_first(client, pool):
    for i in range(3):
        await jobs.enqueue(pool, f"evt-{i}", "BUG-1042", "bug-1042")
    order = [item["ticket_id"] for item in client.get("/jobs").json()]
    assert len(order) == 3
    created = [item["created_at"] for item in client.get("/jobs").json()]
    assert created == sorted(created, reverse=True)


@pytest.mark.parametrize("limit", [0, -1, 101, "abc"])
async def test_list_rejects_invalid_limits(client, limit):
    # 限制取值范围，防止有人请求一百万行把整张表拖出来。
    assert client.get("/jobs", params={"limit": limit}).status_code == 422


async def test_list_honours_the_limit(client, pool):
    for i in range(5):
        await jobs.enqueue(pool, f"evt-{i}", "BUG-1042", "bug-1042")
    assert len(client.get("/jobs", params={"limit": 2}).json()) == 2


# ------------------------------------------------------------ 详情
async def test_detail_of_a_succeeded_job_includes_the_full_report_and_steps(client, pool):
    job = await make_succeeded_job(pool)

    body = client.get(f"/jobs/{job.id}").json()

    assert body["status"] == "succeeded" and body["scenario"] == "bug-1042"
    result = body["result"]
    assert result["report"]["incident_id"] == "BUG-1042"
    assert result["report"]["hypotheses"][0]["evidence"][0]["source"] == "jira"
    assert [s["tool"] for s in result["steps"]] == ["get_ticket", "submit_rca_report"]
    assert result["llm_calls"] == 3 and result["input_tokens"] > 0


async def test_detail_of_a_failed_job_explains_why(client, pool):
    job = await make_failed_job(pool)
    body = client.get(f"/jobs/{job.id}").json()
    assert body["status"] == "failed" and body["result"] is None
    assert "invalid job" in body["last_error"] and body["attempts"] == 1


async def test_detail_of_a_queued_job_reports_when_it_may_run(client, pool):
    job, _ = await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    body = client.get(f"/jobs/{job.id}").json()
    assert body["status"] == "queued" and body["run_after"] is not None and body["result"] is None


async def test_unknown_job_returns_404(client):
    response = client.get("/jobs/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


async def test_a_malformed_id_returns_422_without_touching_the_database(client):
    # 路径参数声明成 UUID 类型，FastAPI 在进入函数之前就拒绝了。
    assert client.get("/jobs/not-a-uuid").status_code == 422
    assert client.get("/jobs/1; DROP TABLE investigation_jobs").status_code == 422


async def test_a_result_in_an_outdated_format_does_not_break_the_endpoints(client, pool):
    # 库里的旧数据和当前代码的结构对不上（比如以后报告结构演进了）：不能让列表接口整个报错。
    job = await make_succeeded_job(pool)
    async with pool.connection() as conn:
        await conn.execute("UPDATE investigation_jobs SET result = '{\"old\": \"format\"}'::jsonb WHERE id = %s", (job.id,))

    listed = client.get("/jobs")
    detail = client.get(f"/jobs/{job.id}")

    assert listed.status_code == 200 and listed.json()[0]["top_cause"] is None
    assert detail.status_code == 200 and detail.json()["result"] is None


async def test_database_outage_returns_503(client, pool):
    await pool.close()  # 模拟数据库不可用
    assert client.get("/jobs").status_code == 503
    assert client.get("/jobs/00000000-0000-0000-0000-000000000000").status_code == 503


def test_without_a_database_pool_the_endpoints_return_503():
    # 应用在"降级模式"下启动（没有连接池）。
    assert TestClient(app).get("/jobs").status_code == 503
