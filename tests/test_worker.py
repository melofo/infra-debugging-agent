"""后台 Worker 的测试（使用真实 PostgreSQL，没启动时自动跳过）。

Worker 的难点全在"异常情况"：模型失败、超时、数据坏了、被取消、数据库断了、多个 worker 并发。
成功的那条路径反而最简单，所以下面大部分测试是在逼它走各种失败路径。
"""

import asyncio

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from app import jobs
from app.config import Settings
from app.llm.base import LLMError, LLMResponse, ToolCall
from app.main import app
from app.worker import Worker
from tests.fakes import SUBMIT_TOOL_NAME, VALID_REPORT_ARGS, AutoLLM, calls, final_text


class FailingLLM:
    def __init__(self, error: Exception):
        self.error = error

    async def generate(self, prompt, system=None):
        raise NotImplementedError

    async def generate_with_tools(self, messages, tools, system=None):
        raise self.error


class HangingLLM:
    """永远不返回的模型，用来测试超时和取消。"""

    def __init__(self):
        self.entered = asyncio.Event()

    async def generate(self, prompt, system=None):
        raise NotImplementedError

    async def generate_with_tools(self, messages, tools, system=None):
        self.entered.set()
        await asyncio.sleep(3600)


def make_settings(**overrides) -> Settings:
    defaults = {
        "_env_file": None,
        "job_timeout_seconds": 5,
        "worker_poll_interval_seconds": 0.05,
        "job_retry_base_delay_seconds": 10,
    }
    return Settings(**{**defaults, **overrides})


async def make_runnable_now(pool):
    """跳过退避等待：把所有排队任务的可执行时间设为现在（模拟时间流逝，不用真的等）。"""
    async with pool.connection() as conn:
        await conn.execute("UPDATE investigation_jobs SET run_after = now()")


async def wait_until(predicate, timeout: float = 5.0):
    """轮询等待某个条件成立，超时就让测试失败。比固定 sleep 更稳也更快。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


# ------------------------------------------------------------------ 成功路径
async def test_worker_investigates_a_job_and_stores_the_report(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    worker = Worker(pool, AutoLLM(), make_settings(), worker_id="w1")

    assert await worker.run_once() is True

    [job] = await jobs.list_jobs(pool)
    assert job.status == "succeeded"
    assert job.result["report"]["incident_id"] == "BUG-1042"
    assert job.result["report"]["hypotheses"][0]["confidence"] == 0.9
    assert [s["tool"] for s in job.result["steps"]] == ["get_ticket", SUBMIT_TOOL_NAME]
    assert job.result["llm_calls"] == 3 and job.result["input_tokens"] > 0
    assert job.locked_by is None and job.finished_at is not None


async def test_run_once_returns_false_when_the_queue_is_empty(pool):
    assert await Worker(pool, AutoLLM(), make_settings()).run_once() is False


# ------------------------------------------------------------------ 失败与重试
async def test_a_model_failure_schedules_a_retry_with_backoff(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    worker = Worker(pool, FailingLLM(LLMError("429 rate limited")), make_settings(), worker_id="w1")

    await worker.run_once()

    [job] = await jobs.list_jobs(pool)
    assert (job.status, job.attempts) == ("queued", 1)
    assert "rate limited" in job.last_error
    assert job.run_after > job.updated_at  # 退避：不会立刻重试
    assert await worker.run_once() is False  # 还没到时间，worker 不会去领它


async def test_a_job_succeeds_on_a_later_attempt_after_a_transient_failure(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    await Worker(pool, FailingLLM(LLMError("overloaded")), make_settings(), worker_id="w1").run_once()
    await make_runnable_now(pool)  # 时间过去了，模型也恢复了

    await Worker(pool, AutoLLM(), make_settings(), worker_id="w2").run_once()

    [job] = await jobs.list_jobs(pool)
    assert (job.status, job.attempts) == ("succeeded", 2)
    assert job.last_error is None  # 成功后清掉旧的错误信息


async def test_a_job_fails_for_good_after_exhausting_its_attempts(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042", max_attempts=2)
    worker = Worker(pool, FailingLLM(LLMError("still down")), make_settings(), worker_id="w1")

    for _ in range(2):
        await make_runnable_now(pool)
        await worker.run_once()

    [job] = await jobs.list_jobs(pool)
    assert (job.status, job.attempts) == ("failed", 2)
    await make_runnable_now(pool)
    assert await worker.run_once() is False  # 最终失败的任务不会再被领取


async def test_a_job_with_missing_data_fails_immediately_without_retries(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-9999", "does-not-exist")
    worker = Worker(pool, AutoLLM(), make_settings(), worker_id="w1")

    await worker.run_once()

    [job] = await jobs.list_jobs(pool)
    # 数据不存在，重试一百次也一样：第 1 次就直接最终失败，剩下的重试次数和模型费用都省下了。
    assert (job.status, job.attempts) == ("failed", 1)
    assert job.last_error == "invalid job: no investigation data for this ticket"
    # 回归测试：错误信息会通过 API 对外展示，不能泄露服务器上的文件路径。
    assert "/" not in job.last_error and "scenarios" not in job.last_error


async def test_an_unexpected_exception_is_recorded_and_retried_not_fatal(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    worker = Worker(pool, FailingLLM(RuntimeError("a bug in our code")), make_settings(), worker_id="w1")

    await worker.run_once()  # 不能抛出来，否则会让整个 worker 循环崩溃

    [job] = await jobs.list_jobs(pool)
    assert job.status == "queued" and "RuntimeError" in job.last_error
    # 意外异常的消息可能含内部细节，对外只显示异常类型，详情只在日志里。
    assert "a bug in our code" not in job.last_error


async def test_a_hanging_investigation_times_out_and_is_retried(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    worker = Worker(pool, HangingLLM(), make_settings(job_timeout_seconds=0.2), worker_id="w1")

    await worker.run_once()

    [job] = await jobs.list_jobs(pool)
    assert job.status == "queued" and "timed out" in job.last_error
    assert job.locked_by is None  # 超时后不会一直占着任务


# ------------------------------------------------------------------ 关闭与取消
async def test_cancelling_the_worker_releases_the_job_without_using_an_attempt(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    llm = HangingLLM()
    worker = Worker(pool, llm, make_settings(job_timeout_seconds=60), worker_id="w1")

    task = asyncio.create_task(worker.run_once())
    await llm.entered.wait()  # 等到它真的在调查中
    assert (await jobs.list_jobs(pool))[0].status == "running"

    task.cancel()  # 模拟服务关闭
    with pytest.raises(asyncio.CancelledError):
        await task

    [job] = await jobs.list_jobs(pool)
    # 放回队列，而且不扣尝试次数：被打断不是任务的错，不该消耗它的重试机会。
    assert (job.status, job.attempts, job.locked_by) == ("queued", 0, None)


# ------------------------------------------------------------------ 主循环
async def test_the_loop_drains_the_queue_and_stops_on_request(pool):
    for i in range(4):
        await jobs.enqueue(pool, f"evt-{i}", "BUG-1042", "bug-1042")
    stop = asyncio.Event()
    task = asyncio.create_task(Worker(pool, AutoLLM(), make_settings()).run(stop))

    async def all_done():
        return all(j.status == "succeeded" for j in await jobs.list_jobs(pool))

    await wait_until(all_done)
    stop.set()
    await asyncio.wait_for(task, 2)  # 收到停止信号后，应该很快正常退出


async def test_the_loop_survives_a_database_outage_and_keeps_working(pool, monkeypatch):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    real_claim = jobs.claim
    attempts = {"n": 0}

    async def flaky_claim(p, worker_id):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise psycopg.OperationalError("connection lost")  # 模拟数据库前两次连不上
        return await real_claim(p, worker_id)

    monkeypatch.setattr("app.worker.jobs.claim", flaky_claim)
    stop = asyncio.Event()
    task = asyncio.create_task(Worker(pool, AutoLLM(), make_settings()).run(stop))

    async def done():
        return (await jobs.list_jobs(pool))[0].status == "succeeded"

    await wait_until(done)  # 数据库恢复后，worker 没有死，照常把任务做完了
    stop.set()
    await asyncio.wait_for(task, 2)
    assert attempts["n"] >= 3


async def test_the_loop_does_not_die_on_unexpected_errors(pool, monkeypatch):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    real_claim = jobs.claim
    first = {"done": False}

    async def buggy_claim(p, worker_id):
        if not first["done"]:
            first["done"] = True
            raise KeyError("a bug nobody expected")
        return await real_claim(p, worker_id)

    monkeypatch.setattr("app.worker.jobs.claim", buggy_claim)
    stop = asyncio.Event()
    task = asyncio.create_task(Worker(pool, AutoLLM(), make_settings()).run(stop))

    async def done():
        return (await jobs.list_jobs(pool))[0].status == "succeeded"

    await wait_until(done)
    stop.set()
    await asyncio.wait_for(task, 2)


async def test_two_workers_process_every_job_exactly_once(pool):
    # 多个 worker 同时工作（比如部署了两个实例）：每个任务只被处理一次，不重复、不遗漏。
    for i in range(10):
        await jobs.enqueue(pool, f"evt-{i}", "BUG-1042", "bug-1042")
    llm = AutoLLM()
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(Worker(pool, llm, make_settings(), worker_id=f"w{n}").run(stop)) for n in (1, 2)
    ]

    async def all_done():
        return all(j.status == "succeeded" for j in await jobs.list_jobs(pool))

    await wait_until(all_done, timeout=10)
    stop.set()
    await asyncio.gather(*tasks)

    all_jobs = await jobs.list_jobs(pool)
    assert all(j.attempts == 1 for j in all_jobs)  # 没有任务被执行第二次
    assert llm.calls == 10 * 3  # 每个任务恰好 3 次模型调用


async def test_a_stale_worker_result_does_not_overwrite_a_newer_one(pool):
    # 围栏在 Worker 层面的体现：worker A 的任务在执行期间被回收并交给了 B，A 做完后写回会被拒绝。
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    stale_job = await jobs.claim(pool, "worker-a")
    async with pool.connection() as conn:
        await conn.execute("UPDATE investigation_jobs SET locked_at = now() - interval '1 hour'")
    await jobs.reclaim_stuck(pool, lease_seconds=600)
    await Worker(pool, AutoLLM(), make_settings(), worker_id="worker-b").run_once()  # B 做完了

    await Worker(pool, AutoLLM(), make_settings(), worker_id="worker-a").process(stale_job)  # A 才醒来

    [job] = await jobs.list_jobs(pool)
    assert job.status == "succeeded" and job.attempts == 2  # 还是 B 的结果


# ------------------------------------------------------------------ 应用启动流程
def test_the_worker_is_disabled_in_the_test_environment(pool):
    # 保护你的钱：测试环境默认不启动 Worker，否则跑测试可能去开发库领任务、调用真实模型。
    with TestClient(app):
        assert app.state.worker_task is None


async def test_the_app_starts_a_worker_and_shuts_it_down_cleanly(pool, monkeypatch):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    base_url = Settings(_env_file=None).database_url
    settings = make_settings(
        worker_enabled=True,
        database_url=make_conninfo(base_url, dbname="agent_test"),  # 用测试库，不碰开发库
    )
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    monkeypatch.setattr("app.main.create_llm_client", lambda s: AutoLLM())  # 假模型，不花钱

    with TestClient(app):
        assert app.state.worker_task is not None

        async def done():
            return (await jobs.list_jobs(pool))[0].status == "succeeded"

        await wait_until(done)
        task = app.state.worker_task

    assert task.done() and not task.cancelled()  # 关闭时 worker 正常结束，不是被强行取消
    assert app.state.pool is None


async def test_without_an_llm_the_worker_is_not_started_and_jobs_wait(pool, monkeypatch):
    from app.llm.factory import LLMNotConfigured

    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    base_url = Settings(_env_file=None).database_url
    settings = make_settings(worker_enabled=True, database_url=make_conninfo(base_url, dbname="agent_test"))
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    def unconfigured(_settings):
        raise LLMNotConfigured("no key")

    monkeypatch.setattr("app.main.create_llm_client", unconfigured)

    with TestClient(app):
        assert app.state.worker_task is None  # 应用照常启动，只是不处理任务

    assert (await jobs.list_jobs(pool))[0].status == "queued"  # 任务还在队列里，等配置好以后处理


async def test_shutdown_releases_a_job_that_is_still_running(pool, monkeypatch):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    llm = HangingLLM()
    base_url = Settings(_env_file=None).database_url
    settings = make_settings(
        worker_enabled=True,
        job_timeout_seconds=60,
        shutdown_grace_seconds=0.3,  # 只给 0.3 秒收尾，而这个任务永远不会结束
        database_url=make_conninfo(base_url, dbname="agent_test"),
    )
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    monkeypatch.setattr("app.main.create_llm_client", lambda s: llm)

    with TestClient(app):
        # 等 worker 真的开始调查了再关闭
        for _ in range(100):
            if llm.entered.is_set() or (await jobs.list_jobs(pool))[0].status == "running":
                break
            await asyncio.sleep(0.05)
        assert (await jobs.list_jobs(pool))[0].status == "running"

    # 关闭超过了宽限期：worker 被取消，任务被放回队列，没有留下"执行中"的僵尸任务。
    [job] = await jobs.list_jobs(pool)
    assert (job.status, job.attempts) == ("queued", 0)


async def test_a_value_error_deep_inside_the_agent_is_retried_not_treated_as_bad_data(pool):
    # 回归测试：以前把任何 ValueError 都当成"数据坏了"而永久失败。
    # 这会把我们自己代码里的 bug 误判成数据问题，既不重试，也掩盖了真正的原因。
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    worker = Worker(pool, FailingLLM(ValueError("bug inside the agent")), make_settings(), worker_id="w1")

    await worker.run_once()

    [job] = await jobs.list_jobs(pool)
    assert job.status == "queued"  # 会重试，而不是永久失败


async def test_corrupt_scenario_data_fails_permanently_with_a_safe_message(pool, tmp_path, monkeypatch):
    broken = tmp_path / "broken-data.json"
    broken.write_text("{ this is not valid json")
    monkeypatch.setattr("app.scenario.SCENARIO_DIR", tmp_path)
    await jobs.enqueue(pool, "evt-1", "BUG-1", "broken-data")

    await Worker(pool, AutoLLM(), make_settings(), worker_id="w1").run_once()

    [job] = await jobs.list_jobs(pool)
    assert job.status == "failed"
    assert job.last_error == "invalid job: investigation data for this ticket is invalid"
    assert str(tmp_path) not in job.last_error
