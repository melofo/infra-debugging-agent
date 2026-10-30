"""数据库层的集成测试（需要本机 PostgreSQL 在运行，没有时自动跳过）。

【单元测试 vs 集成测试】
单元测试用假对象，快、不依赖外部环境；集成测试用真实的数据库，慢一点，
但能验证"我们写的 SQL 和约束在真实数据库里是不是真的有效"。这类东西用假的永远测不出来。
"""

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import init_schema, ping
from app.main import app


async def insert_job(pool, event_id="evt-1", **overrides):
    columns = {"event_id": event_id, "ticket_id": "BUG-1042", "scenario": "bug-1042", **overrides}
    names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    async with pool.connection() as conn:
        # %s 是参数占位符：值由驱动安全地传给数据库，【永远不要】用字符串拼接把值写进 SQL，
        # 那是 SQL 注入漏洞的根源（Java 里对应：用 PreparedStatement，别拼接字符串）。
        cursor = await conn.execute(
            f"INSERT INTO investigation_jobs ({names}) VALUES ({placeholders}) RETURNING id",
            tuple(columns.values()),
        )
        return (await cursor.fetchone())[0]


async def test_ping_succeeds_on_a_live_database(pool):
    assert await ping(pool) is True


async def test_new_job_gets_sensible_defaults(pool):
    job_id = await insert_job(pool)
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT status, attempts, max_attempts, locked_at, result, run_after <= now() "
                "FROM investigation_jobs WHERE id = %s",
                (job_id,),
            )
        ).fetchone()
    status, attempts, max_attempts, locked_at, result, runnable_now = row
    assert (status, attempts, max_attempts) == ("queued", 0, 3)
    assert locked_at is None and result is None
    assert runnable_now is True  # 新任务立刻可以被执行


async def test_schema_creation_is_idempotent(pool):
    # 应用每次启动都会执行建表脚本，所以重复执行必须安全。
    await init_schema(pool)
    await init_schema(pool)


async def test_duplicate_event_id_is_rejected_by_the_database(pool):
    await insert_job(pool, event_id="evt-dup")
    # 幂等性的根基：重复事件在数据库层面就插不进去，而不是靠应用代码"先查后插"
    # （那种做法在两个请求同时到达时有竞态条件，会漏掉去重）。
    with pytest.raises(psycopg.errors.UniqueViolation):
        await insert_job(pool, event_id="evt-dup")


async def test_invalid_status_is_rejected_by_the_database(pool):
    with pytest.raises(psycopg.errors.CheckViolation):
        await insert_job(pool, event_id="evt-bad", status="exploded")


async def test_failed_transaction_leaves_no_partial_data(pool):
    # 事务的原子性：块里抛异常，之前的写入会一起回滚。
    with pytest.raises(RuntimeError):
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO investigation_jobs (event_id, ticket_id, scenario) VALUES ('evt-tx', 'T', 's')"
            )
            raise RuntimeError("something went wrong after the insert")
    async with pool.connection() as conn:
        count = (await (await conn.execute("SELECT count(*) FROM investigation_jobs")).fetchone())[0]
    assert count == 0


def test_ready_returns_503_without_a_database_pool():
    # 没有启动流程（lifespan）就没有连接池：就绪检查应当如实报告"不可用"，而不是崩溃。
    response = TestClient(app).get("/ready")
    assert response.status_code == 503


def test_full_startup_connects_to_the_database_and_reports_ready(pool):
    # 这里依赖 `pool` fixture 只是为了"数据库没启动时自动跳过"。
    # `with TestClient(app)` 会真正执行应用的启动流程（lifespan）：创建连接池、连接开发库、建表。
    # （不加 with 的 TestClient 不会执行启动流程，所以上面的其他测试里 app 没有连接池。）
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_liveness_does_not_depend_on_the_database():
    # /health 只回答"进程活着吗"，不检查数据库（见 health.py 的说明）。
    assert TestClient(app).get("/health").status_code == 200


def test_app_starts_in_degraded_mode_when_the_database_is_down(monkeypatch):
    # 降级运行：数据库连不上时，应用仍然能启动，不依赖数据库的功能不受影响，
    # 只有 /ready 如实报告 503。端口 59999 上没有任何服务，模拟"数据库挂了"。
    broken = Settings(
        _env_file=None,
        database_url="postgresql://agent:x@127.0.0.1:59999/agent",
        db_pool_timeout_seconds=0.5,
    )
    monkeypatch.setattr("app.main.get_settings", lambda: broken)
    with TestClient(app) as client:  # 启动流程里建表会失败，但只记录错误，不让应用崩溃
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503


def test_shutdown_leaves_no_closed_pool_behind():
    # 回归测试：启动流程跑完之后，app.state.pool 必须被清掉，不能留着一个已关闭的池
    # （这个问题曾经让后面的测试莫名其妙地失败）。
    with TestClient(app):
        pass
    assert app.state.pool is None
