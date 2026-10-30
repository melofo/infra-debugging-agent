"""任务队列的集成测试，使用真实的 PostgreSQL（没启动时自动跳过）。

队列的正确性核心是【并发】：多个 worker 同时操作，不能重复领取、不能丢任务。
这种性质用假对象永远测不出来，必须用真实数据库、真实的并发连接。
"""

import asyncio

import pytest

from app import jobs
from app.jobs import Job


async def sql(pool, statement: str, params: tuple = ()):
    """测试辅助：直接执行 SQL（用来"拨动时钟"，模拟时间流逝，而不是真的等）。"""
    async with pool.connection() as conn:
        await conn.execute(statement, params)


async def enqueue_n(pool, n: int) -> list[Job]:
    return [(await jobs.enqueue(pool, f"evt-{i}", "BUG-1042", "bug-1042"))[0] for i in range(n)]


# ---------------------------------------------------------------- 入队 / 幂等
async def test_enqueue_creates_a_queued_job(pool):
    job, created = await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    assert created is True
    assert (job.status, job.attempts, job.max_attempts) == ("queued", 0, 3)
    assert job.ticket_id == "BUG-1042"


async def test_enqueue_same_event_twice_returns_the_existing_job(pool):
    first, created_first = await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    second, created_second = await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")

    assert (created_first, created_second) == (True, False)
    assert second.id == first.id
    assert len(await jobs.list_jobs(pool)) == 1


async def test_concurrent_duplicate_enqueues_create_exactly_one_job(pool):
    # 模拟 Jira 在 20 个连接上同时重发同一个事件：必须只有一个"新建"，不能出现重复任务。
    results = await asyncio.gather(*[jobs.enqueue(pool, "evt-same", "BUG-1042", "bug-1042") for _ in range(20)])

    assert sum(1 for _, created in results if created) == 1
    assert len({job.id for job, _ in results}) == 1
    assert len(await jobs.list_jobs(pool)) == 1


async def test_job_model_matches_every_column_of_the_table(pool):
    # 手写 SQL 没有 ORM 保证"类和表一致"，这个测试就是兜底：
    # SELECT * 拿到表的全部列，如果有人给表加了列而忘了改 Job，这里会立刻报错。
    job, _ = await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    async with pool.connection() as conn:
        cursor = await conn.execute("SELECT * FROM investigation_jobs WHERE id = %s", (job.id,))
        column_names = {column.name for column in cursor.description}
    assert column_names == set(Job.model_fields)


# ---------------------------------------------------------------- 领取
async def test_claim_marks_the_job_running(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    job = await jobs.claim(pool, "worker-a")

    assert job is not None
    assert (job.status, job.attempts, job.locked_by) == ("running", 1, "worker-a")
    assert job.locked_at is not None


async def test_claim_returns_none_when_the_queue_is_empty(pool):
    assert await jobs.claim(pool, "worker-a") is None


async def test_claim_takes_the_oldest_job_first(pool):
    first, _ = await jobs.enqueue(pool, "evt-1", "BUG-1", "bug-1042")
    await jobs.enqueue(pool, "evt-2", "BUG-2", "bug-1042")
    claimed = await jobs.claim(pool, "worker-a")
    assert claimed.id == first.id


async def test_claim_ignores_jobs_scheduled_for_the_future(pool):
    job, _ = await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    await sql(pool, "UPDATE investigation_jobs SET run_after = now() + interval '1 hour' WHERE id = %s", (job.id,))
    assert await jobs.claim(pool, "worker-a") is None


async def test_a_claimed_job_cannot_be_claimed_again(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    assert await jobs.claim(pool, "worker-a") is not None
    assert await jobs.claim(pool, "worker-b") is None


async def test_concurrent_workers_never_claim_the_same_job(pool):
    # 这是整个队列最重要的测试：20 个任务，8 个 worker 同时疯狂领取，直到队列空。
    # 要求：每个任务被领取恰好一次，没有重复，也没有遗漏。
    await enqueue_n(pool, 20)

    async def worker(name: str) -> list:
        claimed = []
        while (job := await jobs.claim(pool, name)) is not None:
            claimed.append(job.id)
            await asyncio.sleep(0)  # 让出控制权，增加不同 worker 交错执行的机会
        return claimed

    results = await asyncio.gather(*[worker(f"worker-{i}") for i in range(8)])
    all_claimed = [job_id for claimed in results for job_id in claimed]

    assert len(all_claimed) == 20
    assert len(set(all_claimed)) == 20  # 没有任何一个任务被领取两次


# ---------------------------------------------------------------- 完成
async def test_complete_stores_the_result_and_releases_the_lock(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    claimed = await jobs.claim(pool, "worker-a")

    done = await jobs.complete(pool, claimed.id, "worker-a", claimed.attempts, {"report": {"summary": "pool size"}})

    assert done.status == "succeeded"
    assert done.result == {"report": {"summary": "pool size"}}  # jsonb 自动还原成 Python 字典
    assert done.locked_by is None and done.finished_at is not None


async def test_a_worker_that_does_not_hold_the_job_cannot_complete_it(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    claimed = await jobs.claim(pool, "worker-a")

    assert await jobs.complete(pool, claimed.id, "worker-b", claimed.attempts, {"x": 1}) is None
    assert (await jobs.get_job(pool, claimed.id)).status == "running"  # 任务没有被改动


# ---------------------------------------------------------------- 失败 / 重试 / 退避
async def test_failure_requeues_the_job_with_a_delay(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    claimed = await jobs.claim(pool, "worker-a")

    failed = await jobs.fail(pool, claimed.id, "worker-a", claimed.attempts, "model rate limited")

    assert failed.status == "queued"
    assert failed.last_error == "model rate limited"
    assert failed.locked_by is None
    assert failed.run_after > failed.updated_at  # 退避：不会立刻被重新领取
    assert await jobs.claim(pool, "worker-b") is None


async def test_backoff_doubles_with_each_attempt_and_is_capped(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042", max_attempts=10)
    delays = []
    for _ in range(5):
        await sql(pool, "UPDATE investigation_jobs SET run_after = now()")  # 跳过等待，让它立刻可领取
        claimed = await jobs.claim(pool, "worker-a")
        failed = await jobs.fail(
            pool, claimed.id, "worker-a", claimed.attempts, "boom", base_delay_seconds=10, max_delay_seconds=50
        )
        delays.append(round((failed.run_after - failed.updated_at).total_seconds()))

    assert delays == [10, 20, 40, 50, 50]  # 翻倍增长，到 50 封顶


async def test_job_fails_permanently_when_attempts_are_exhausted(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042", max_attempts=2)

    for attempt in (1, 2):
        await sql(pool, "UPDATE investigation_jobs SET run_after = now()")
        claimed = await jobs.claim(pool, "worker-a")
        assert claimed.attempts == attempt
        last = await jobs.fail(pool, claimed.id, "worker-a", claimed.attempts, f"error {attempt}")

    assert last.status == "failed"
    assert last.last_error == "error 2"
    assert last.finished_at is not None
    assert await jobs.claim(pool, "worker-a") is None  # 最终失败的任务不会再被领取


# ---------------------------------------------------------------- 回收 / 围栏
async def test_reclaim_requeues_jobs_whose_worker_disappeared(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    claimed = await jobs.claim(pool, "worker-a")
    await sql(pool, "UPDATE investigation_jobs SET locked_at = now() - interval '1 hour' WHERE id = %s", (claimed.id,))

    assert await jobs.reclaim_stuck(pool, lease_seconds=600) == 1

    job = await jobs.get_job(pool, claimed.id)
    assert (job.status, job.locked_by) == ("queued", None)
    assert "lease expired" in job.last_error
    assert (await jobs.claim(pool, "worker-b")).attempts == 2  # 立刻可以被别的 worker 领走


async def test_reclaim_leaves_healthy_running_jobs_alone(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    await jobs.claim(pool, "worker-a")  # 刚领走，租约还很新

    assert await jobs.reclaim_stuck(pool, lease_seconds=600) == 0


async def test_reclaim_fails_jobs_that_have_no_attempts_left(pool):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042", max_attempts=1)
    claimed = await jobs.claim(pool, "worker-a")
    await sql(pool, "UPDATE investigation_jobs SET locked_at = now() - interval '1 hour' WHERE id = %s", (claimed.id,))

    await jobs.reclaim_stuck(pool, lease_seconds=600)

    assert (await jobs.get_job(pool, claimed.id)).status == "failed"


async def test_fencing_stops_a_stale_worker_from_overwriting_a_newer_result(pool):
    # 场景：worker A 领了任务后卡住 -> 任务被回收 -> worker B 领走并完成 -> A 终于醒来想写回结果。
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    a = await jobs.claim(pool, "worker-a")
    await sql(pool, "UPDATE investigation_jobs SET locked_at = now() - interval '1 hour' WHERE id = %s", (a.id,))
    await jobs.reclaim_stuck(pool, lease_seconds=600)
    b = await jobs.claim(pool, "worker-b")

    assert await jobs.complete(pool, b.id, "worker-b", b.attempts, {"from": "b"}) is not None
    # A 的写入被拒绝（围栏）：它既不是当前持有者，任务也不再是 running。
    assert await jobs.complete(pool, a.id, "worker-a", a.attempts, {"from": "a"}) is None
    assert (await jobs.get_job(pool, a.id)).result == {"from": "b"}


async def test_stale_attempt_number_is_rejected_even_for_the_same_worker(pool):
    # 同一个 worker 先后领到同一个任务的两次尝试：旧的那次尝试不能修改新的状态。
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    first = await jobs.claim(pool, "worker-a")
    await sql(pool, "UPDATE investigation_jobs SET locked_at = now() - interval '1 hour' WHERE id = %s", (first.id,))
    await jobs.reclaim_stuck(pool, lease_seconds=600)
    second = await jobs.claim(pool, "worker-a")  # 同一个 worker，第 2 次尝试
    assert second.attempts == 2

    assert await jobs.complete(pool, first.id, "worker-a", first.attempts, {"stale": True}) is None
    assert await jobs.complete(pool, second.id, "worker-a", second.attempts, {"fresh": True}) is not None


async def test_list_jobs_returns_newest_first(pool):
    await enqueue_n(pool, 3)
    listed = await jobs.list_jobs(pool, limit=2)
    assert [j.event_id for j in listed] == ["evt-2", "evt-1"]


@pytest.mark.parametrize("bad", ["", "x" * 5000])
async def test_fail_truncates_very_long_error_messages(pool, bad):
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    claimed = await jobs.claim(pool, "worker-a")
    failed = await jobs.fail(pool, claimed.id, "worker-a", claimed.attempts, bad)
    assert len(failed.last_error) <= 2000
