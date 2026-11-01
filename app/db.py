"""PostgreSQL 连接管理。

我们用 psycopg（PostgreSQL 的 Python 驱动）直接写 SQL，不用 ORM。
对 Java 开发者：psycopg 相当于 JDBC 驱动，连接池（AsyncConnectionPool）相当于 HikariCP。
选择直接写 SQL 的原因：任务队列的核心就是几条精心设计的 SQL（见 Phase 4 第 2 步），
每一条都要看得见；后面做 pgvector 向量检索也是写原生 SQL。

【为什么需要连接池】
建立一次数据库连接很贵（TCP 握手、认证、会话初始化）。池里预先保持几条连接，
请求来了借一条，用完还回去。和 HikariCP 是同一个概念。
"""

import logging
from pathlib import Path

import psycopg
from fastapi import HTTPException, Request
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
KNOWLEDGE_SCHEMA_PATH = Path(__file__).with_name("schema_knowledge.sql")


def create_pool(database_url: str, max_size: int = 5, timeout: float = 5.0) -> AsyncConnectionPool:
    """创建连接池（但还没打开，需要调用 await pool.open()）。"""
    return AsyncConnectionPool(
        database_url,
        min_size=1,
        max_size=max_size,
        # open=False：构造时不立刻连接。连接由应用的启动流程统一打开（见 main.py 的 lifespan）。
        open=False,
        # 借连接最多等 timeout 秒，借不到就报错，而不是一直卡住（数据库挂了的时候，让请求尽快失败）。
        timeout=timeout,
    )


# 建表时用的咨询锁编号（任意固定的整数，只要全应用统一）。
SCHEMA_LOCK_KEY = 7_315_001


async def _schema_lock(conn) -> None:
    """让"同时启动的多个实例"排队建表，一个一个来。

    问题：CREATE TABLE IF NOT EXISTS 看起来是幂等的，但两个连接【同时】执行时，两边都发现表不存在，
    都去创建，后到的那个会因为系统目录里的唯一约束冲突而报错（pg_type_typname_nsp_index），CREATE EXTENSION 同理。
    在 Cloud Run 这样会同时拉起好几个实例的环境里，这不是理论上的问题，而是每次发布都会碰到。
    更糟的是知识库那一步会把这个错误当成"pgvector 不可用"悄悄吞掉：输掉竞争的实例会在没有语义检索的状态下一直运行，没人发现。

    解法：事务级咨询锁 pg_advisory_xact_lock。拿不到锁的会等到持有者提交，然后发现表已经存在，什么也不做。
    "xact" 版本在事务结束（提交或回滚）时自动释放，进程崩溃也不会留下永远不放的锁。
    """
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))


async def init_schema(pool: AsyncConnectionPool) -> None:
    """执行 schema.sql 建表。所有语句都是 IF NOT EXISTS，重复执行是安全的。"""
    # `async with pool.connection() as conn` 的含义：从池里借一条连接，用完自动还回去。
    # psycopg 默认在一个事务里执行语句；正常退出这个 with 块时自动 COMMIT，抛异常则自动 ROLLBACK。
    # （和 JDBC 里 autoCommit=false 然后手动 commit/rollback 是同一回事，只是自动化了。）
    async with pool.connection() as conn:
        await _schema_lock(conn)
        await conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


async def init_knowledge_schema(pool: AsyncConnectionPool) -> bool:
    """建知识库的表（需要 pgvector）。成功返回 True；pgvector 不可用时返回 False，而不是抛异常。

    知识库检索是"增强功能"：它不可用时，任务队列、聊天、Webhook 都应该照常工作。
    所以这里把失败转成返回值，由调用方决定怎么降级（比如改用关键词检索）。
    """
    try:
        async with pool.connection() as conn:
            await _schema_lock(conn)
            await conn.execute(KNOWLEDGE_SCHEMA_PATH.read_text(encoding="utf-8"))
        return True
    except psycopg.Error:
        logger.warning("knowledge schema unavailable (is the pgvector extension installed?)", exc_info=True)
        return False


async def ping(pool: AsyncConnectionPool) -> bool:
    """数据库能不能用：执行一条最简单的查询。"""
    try:
        async with pool.connection(timeout=2) as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception:
        logger.warning("database ping failed", exc_info=True)
        return False


def get_pool(request: Request) -> AsyncConnectionPool:
    """FastAPI 依赖：取出应用启动时创建的连接池。

    app.state 是 FastAPI 提供的"应用级共享容器"，生命周期和应用一样长（类似 Spring 的单例 Bean）。
    """
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        # 503：服务暂时不可用。数据库相关的功能不可用，但不影响别的接口。
        raise HTTPException(status_code=503, detail="database is not available")
    return pool
