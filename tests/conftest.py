"""pytest 的共享配置文件：conftest.py 里定义的 fixture，同一目录下所有测试文件都能直接使用。

（类似 JUnit 里放在基类或 @ExtendWith 扩展里的公共准备逻辑。）
"""

import os

# 【必须放在所有 import 之前】测试环境默认关闭后台 Worker。
# 一些测试会跑完整的应用启动流程；如果 Worker 随之启动，它会去开发库里领任务，
# 用你 .env 里的真实 API key 调用真实模型，花真钱。环境变量的优先级高于 .env 文件，
# 所以这里设置了之后，即使 .env 里写了 WORKER_ENABLED=true 也不会生效。
# 需要测试 Worker 启动的测试，会显式传入自己的配置。
os.environ["WORKER_ENABLED"] = "false"
# 同样，自动化测试默认关闭接口认证（环境变量优先于 .env）；专门测认证的测试会显式打开它。
os.environ["AUTH_REQUIRED"] = "false"

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.db import create_pool, init_knowledge_schema, init_schema

TEST_DB_NAME = "agent_test"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture
async def pool():
    """连接到【专用测试库】的连接池，每个测试开始前表是空的。

    三道安全措施，因为这些测试会清空表：
      1. 用独立的 agent_test 库，不碰开发库 agent；
      2. 只允许连本机的数据库，防止配置错误时清空了别处的真实数据；
      3. 数据库没启动时，测试自动【跳过】而不是失败，这样没装 Docker 的人也能跑其他测试。
    """
    base_url = Settings(_env_file=None).database_url
    host = conninfo_to_dict(base_url).get("host")
    if host not in LOCAL_HOSTS:
        pytest.skip(f"refusing to run destructive DB tests against non-local host {host!r}")

    try:
        # CREATE DATABASE 不能在事务里执行，所以这里要开启 autocommit。
        async with await psycopg.AsyncConnection.connect(base_url, autocommit=True, connect_timeout=3) as admin:
            exists = await (
                await admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_NAME,))
            ).fetchone()
            if not exists:
                await admin.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    except psycopg.OperationalError:
        pytest.skip("PostgreSQL is not running (start it with: docker compose up -d db)")

    test_pool: AsyncConnectionPool = create_pool(make_conninfo(base_url, dbname=TEST_DB_NAME))
    await test_pool.open(wait=True, timeout=5)
    await init_schema(test_pool)
    async with test_pool.connection() as conn:
        await conn.execute("TRUNCATE investigation_jobs CASCADE")  # CASCADE：连带清空引用它的 proposed_actions
    yield test_pool  # yield 之前是准备，之后是清理
    await test_pool.close()


@pytest.fixture
async def knowledge_pool(pool):
    """在 pool 的基础上准备好知识库的表（需要 pgvector），每个测试开始前表是空的。"""
    if not await init_knowledge_schema(pool):
        pytest.skip("pgvector extension is not available in this PostgreSQL")
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE knowledge_documents CASCADE")  # CASCADE 连带清空 knowledge_chunks
    return pool
