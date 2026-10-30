"""基于 PostgreSQL 的任务队列。

对应 app/schema.sql 里的 investigation_jobs 表。Job 模型和表的列一一对应：
手写 SQL（不用 ORM）意味着"表 <-> Python 对象"的映射要自己维护，
所以 tests/test_jobs.py 里有一个测试，用真实数据库的一行数据确认 Job 能完整解析，列对不上会立刻报错。

【队列的生命周期（状态机）】

    enqueue            claim              complete
   ---------> queued ---------> running ----------> succeeded
                 ^                 |
                 |   fail (还有重试次数，等退避时间后)
                 +-----------------+
                                   |   fail (次数用完)
                                   +---------------> failed

【交付语义：至少一次（at-least-once）】
worker 可能在做完任务之后、标记完成之前崩溃，这个任务会被回收并再执行一次。
所以任务的处理逻辑必须容忍"被执行不止一次"。我们的调查是只读的，重复执行没有副作用（只多花一点钱），
所以这是可以接受的。如果任务会写生产环境（比如真的回滚发布），就必须额外做幂等处理，这也是第一版只读的原因之一。

【为什么每个操作都是一条 SQL】
单条 SQL 语句在数据库里是原子的：要么整体生效，要么完全不生效，中间不会被别的连接看到。
把"检查状态 + 修改状态"合并成一条语句，就消除了"先查后改"之间的竞态条件。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from psycopg.rows import class_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel


class Job(BaseModel):
    """investigation_jobs 表的一行。字段名必须和列名完全一致（class_row 靠名字对应）。"""

    id: UUID
    event_id: str
    ticket_id: str
    scenario: str
    status: Literal["queued", "running", "succeeded", "failed"]
    attempts: int
    max_attempts: int
    run_after: datetime
    locked_at: datetime | None
    locked_by: str | None
    last_error: str | None
    result: dict | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


# ---------------------------------------------------------------------------
# 入队（带幂等）
# ---------------------------------------------------------------------------
async def enqueue(
    pool: AsyncConnectionPool,
    event_id: str,
    ticket_id: str,
    scenario: str,
    max_attempts: int = 3,
) -> tuple[Job, bool]:
    """把一个调查任务放进队列。返回 (任务, 是否新建)。

    同一个 event_id 重复入队时，不会产生第二个任务，而是返回已有的那个（是否新建 = False）。
    这就是 Webhook 的幂等：Jira 重发同一个事件，我们不会重复调查。
    """
    async with pool.connection() as conn, conn.cursor(row_factory=class_row(Job)) as cur:
        # 最多重试几次是为了处理一个极罕见的情况，见下面的注释。
        for _ in range(3):
            # ON CONFLICT (event_id) DO NOTHING：遇到唯一约束冲突时不报错、什么也不做。
            # RETURNING *：把刚插入的行返回。冲突时没有插入，所以没有返回行。
            # 这一条 SQL 把"判断是否已存在"和"插入"合成了一步，并发下也不会重复：
            # 两个请求同时插入同一个 event_id，数据库保证只有一个成功。
            await cur.execute(
                """
                INSERT INTO investigation_jobs (event_id, ticket_id, scenario, max_attempts)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING *
                """,
                (event_id, ticket_id, scenario, max_attempts),
            )
            created = await cur.fetchone()
            if created is not None:
                return created, True

            await cur.execute("SELECT * FROM investigation_jobs WHERE event_id = %s", (event_id,))
            existing = await cur.fetchone()
            if existing is not None:
                return existing, False
            # 极罕见：冲突的那个事务在我们查询之前回滚了，行又不存在了。再试一次插入即可。
        raise RuntimeError(f"could not enqueue event {event_id!r}")


# ---------------------------------------------------------------------------
# 领取任务：整个队列最关键的一条 SQL
# ---------------------------------------------------------------------------
async def claim(pool: AsyncConnectionPool, worker_id: str) -> Job | None:
    """领取一个可执行的任务并标记为 running。没有任务时返回 None。

    【问题】多个 worker 同时来领任务，怎么保证不会领到同一个？
    错误做法：先 SELECT 一个排队中的任务，再 UPDATE 成 running。
      两个 worker 可能同时 SELECT 到同一行，然后都 UPDATE 成功，任务被执行两次。
    笨办法：用 FOR UPDATE 锁住那一行。但第二个 worker 会【阻塞等待】第一个提交，
      然后发现任务已经被领走，白白浪费时间，还可能一直排队。

    【正确做法】FOR UPDATE SKIP LOCKED：
      "锁住我选中的行；如果某行已经被别人锁住了，直接跳过它去找下一行，不要等。"
      所以 worker A 锁住任务 1，worker B 同时来时自动跳过任务 1，领到任务 2。互不阻塞，也不会重复。
    这是用数据库做任务队列的标准技巧，PostgreSQL 从 9.5 版本开始支持。

    整个"选择 + 修改"写成一条语句（CTE，即 WITH 子句），在同一个原子操作里完成。
    """
    async with pool.connection() as conn, conn.cursor(row_factory=class_row(Job)) as cur:
        await cur.execute(
            """
            WITH next AS (
                SELECT id FROM investigation_jobs
                WHERE status = 'queued' AND run_after <= now()   -- 排队中，并且已经到了可执行时间
                ORDER BY run_after, created_at                   -- 先到先得
                LIMIT 1
                FOR UPDATE SKIP LOCKED                           -- 关键：被别人锁住的行直接跳过
            )
            UPDATE investigation_jobs AS j
            SET status = 'running',
                attempts = j.attempts + 1,
                locked_at = now(),
                locked_by = %s,
                updated_at = now()
            FROM next
            WHERE j.id = next.id
            RETURNING j.*
            """,
            (worker_id,),
        )
        return await cur.fetchone()


# ---------------------------------------------------------------------------
# 完成 / 失败
# ---------------------------------------------------------------------------
# 注意下面两个函数的 WHERE 条件里都有 locked_by 和 attempts：这叫"围栏"（fencing）。
# 场景：worker A 领了任务但卡住很久，任务被回收后又被 worker B 领走并完成了；
# 这时 A 终于醒了，想把自己的（过期的）结果写回去。如果没有围栏，A 会覆盖 B 的结果。
# 有了围栏：只有"当前持有者、当前这一次尝试"才能修改这个任务，过期的 worker 会发现自己更新了 0 行，
# 函数返回 None，worker 据此知道"我已经出局了"。


async def complete(
    pool: AsyncConnectionPool, job_id: UUID, worker_id: str, attempt: int, result: dict
) -> Job | None:
    """标记任务成功并保存结果。如果这个 worker 已经不再持有该任务（被回收了），返回 None。"""
    async with pool.connection() as conn, conn.cursor(row_factory=class_row(Job)) as cur:
        await cur.execute(
            """
            UPDATE investigation_jobs
            SET status = 'succeeded', result = %s, last_error = NULL,
                locked_at = NULL, locked_by = NULL, finished_at = now(), updated_at = now()
            WHERE id = %s AND status = 'running' AND locked_by = %s AND attempts = %s
            RETURNING *
            """,
            (Jsonb(result), job_id, worker_id, attempt),  # Jsonb 把 Python 字典转成 PostgreSQL 的 jsonb
        )
        return await cur.fetchone()


async def fail(
    pool: AsyncConnectionPool,
    job_id: UUID,
    worker_id: str,
    attempt: int,
    error: str,
    base_delay_seconds: float = 10.0,
    max_delay_seconds: float = 300.0,
    permanent: bool = False,
) -> Job | None:
    """记录一次失败。还有重试次数就重新排队（带退避），否则标记为最终失败。

    permanent=True 表示"重试也没用"的失败（比如任务引用的数据根本不存在），直接标记最终失败，
    不再浪费重试次数和模型费用。能不能靠重试解决，要由调用方根据错误类型来判断。

    【指数退避】第 1 次失败等 10 秒，第 2 次 20 秒，第 3 次 40 秒……最多不超过 5 分钟。
    为什么不立刻重试？失败往往是因为下游暂时有问题（比如模型服务限流），
    马上重试大概率还是失败，还会加重对方的压力。等一会儿再试成功率更高。
    （生产里通常还会加"抖动"，在等待时间上加随机量，避免很多任务同时重试。这里为了测试可预测先省略。）
    """
    async with pool.connection() as conn, conn.cursor(row_factory=class_row(Job)) as cur:
        await cur.execute(
            """
            UPDATE investigation_jobs
            SET status = CASE WHEN attempts >= max_attempts OR %(permanent)s::boolean THEN 'failed' ELSE 'queued' END,
                run_after = CASE
                    WHEN attempts >= max_attempts OR %(permanent)s::boolean THEN run_after
                    ELSE now() + make_interval(secs => LEAST(%(max_delay)s::float8,
                                                              %(base_delay)s::float8 * power(2, attempts - 1)))
                END,
                finished_at = CASE WHEN attempts >= max_attempts OR %(permanent)s::boolean THEN now() END,
                last_error = %(error)s,
                locked_at = NULL, locked_by = NULL, updated_at = now()
            WHERE id = %(id)s AND status = 'running' AND locked_by = %(worker)s AND attempts = %(attempt)s
            RETURNING *
            """,
            {
                "id": job_id,
                "worker": worker_id,
                "attempt": attempt,
                "error": error[:2000],  # 错误信息可能很长，截断，避免撑大数据库
                "base_delay": base_delay_seconds,
                "max_delay": max_delay_seconds,
                "permanent": permanent,
            },
        )
        return await cur.fetchone()


async def release(pool: AsyncConnectionPool, job_id: UUID, worker_id: str, attempt: int) -> Job | None:
    """把领走但还没做完的任务原样放回队列，并且【不扣】这次尝试次数。

    用于 worker 被要求关闭的时候：这不是任务的错，不应该消耗它的重试机会。
    和 fail 的区别：fail 表示"尝试了，失败了"，会计入次数、带退避；release 表示"我没来得及做"。
    """
    async with pool.connection() as conn, conn.cursor(row_factory=class_row(Job)) as cur:
        await cur.execute(
            """
            UPDATE investigation_jobs
            SET status = 'queued', attempts = GREATEST(attempts - 1, 0), run_after = now(),
                last_error = 'released: worker shutting down',
                locked_at = NULL, locked_by = NULL, updated_at = now()
            WHERE id = %s AND status = 'running' AND locked_by = %s AND attempts = %s
            RETURNING *
            """,
            (job_id, worker_id, attempt),
        )
        return await cur.fetchone()


# ---------------------------------------------------------------------------
# 回收卡死的任务
# ---------------------------------------------------------------------------
async def reclaim_stuck(pool: AsyncConnectionPool, lease_seconds: float) -> int:
    """把"领走太久还没结果"的任务收回来，返回回收了多少个。

    【租约（lease）】worker 领任务，相当于租了一段时间。如果 worker 崩溃了、断电了，
    任务会永远停在 running，没有人会处理它。所以要定期检查：
    locked_at 距今超过 lease_seconds 的 running 任务，认为 worker 已经丢失，收回来：
      - 还有重试次数：重新排队，立刻可以被领取；
      - 次数用完：标记最终失败。
    lease_seconds 必须比一次调查的正常耗时长得多，否则正在正常工作的任务会被误回收，造成重复执行。
    （误回收时，围栏机制保证了旧 worker 不会覆盖新结果。）
    """
    async with pool.connection() as conn:
        cursor = await conn.execute(
            """
            UPDATE investigation_jobs
            SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
                run_after = now(),
                finished_at = CASE WHEN attempts >= max_attempts THEN now() END,
                last_error = 'worker lost: lease expired',
                locked_at = NULL, locked_by = NULL, updated_at = now()
            WHERE status = 'running' AND locked_at < now() - make_interval(secs => %s::float8)
            """,
            (lease_seconds,),
        )
        return cursor.rowcount


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
async def get_job(pool: AsyncConnectionPool, job_id: UUID) -> Job | None:
    async with pool.connection() as conn, conn.cursor(row_factory=class_row(Job)) as cur:
        await cur.execute("SELECT * FROM investigation_jobs WHERE id = %s", (job_id,))
        return await cur.fetchone()


async def list_jobs(pool: AsyncConnectionPool, limit: int = 50) -> list[Job]:
    async with pool.connection() as conn, conn.cursor(row_factory=class_row(Job)) as cur:
        await cur.execute("SELECT * FROM investigation_jobs ORDER BY created_at DESC LIMIT %s", (limit,))
        return await cur.fetchall()
