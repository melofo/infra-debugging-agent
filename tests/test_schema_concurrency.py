"""多个实例同时启动时，建表不能互相打架（真实 PostgreSQL，没启动时自动跳过）。

每一轮都在【全新的临时数据库】里并发地跑建表：只有在表还不存在的时候才会出现竞争，
所以不能用已经建好表的测试库。临时库用完就删。
"""

import asyncio
import logging

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app import db
from app.config import Settings
from app.db import create_pool, init_knowledge_schema, init_schema
from tests.db_guard import unavailable

STARTERS = 8
ROUNDS = 4


@pytest.fixture
async def fresh_database():
    base = Settings(_env_file=None).database_url
    if conninfo_to_dict(base).get("host") not in {"127.0.0.1", "localhost", "::1"}:
        unavailable("refusing to create databases on a non-local host")
    created: list[str] = []

    async def make() -> str:
        name = f"schema_race_{len(created)}"
        async with await psycopg.AsyncConnection.connect(base, autocommit=True, connect_timeout=3) as admin:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
            await admin.execute(f'CREATE DATABASE "{name}"')
        created.append(name)
        return make_conninfo(base, dbname=name)

    try:
        await psycopg.AsyncConnection.connect(base, connect_timeout=3)
    except psycopg.OperationalError:
        unavailable("PostgreSQL is not running (start it with: docker compose up -d db)")
    yield make
    async with await psycopg.AsyncConnection.connect(base, autocommit=True) as admin:
        for name in created:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


async def start_instances(url: str):
    pools = [create_pool(url, max_size=2) for _ in range(STARTERS)]
    for p in pools:
        await p.open(wait=True, timeout=5)
    try:
        # 每个"实例"启动时都会依次做这两步（见 app/main.py 的 lifespan）：8 个实例同时做
        schema = await asyncio.gather(*(init_schema(p) for p in pools), return_exceptions=True)
        knowledge = await asyncio.gather(*(init_knowledge_schema(p) for p in pools), return_exceptions=True)
        return schema, knowledge
    finally:
        for p in pools:
            await p.close()


async def test_many_instances_starting_at_once_all_succeed_and_all_get_the_knowledge_tables(fresh_database, caplog):
    caplog.set_level(logging.WARNING)
    for _ in range(ROUNDS):
        schema, knowledge = await start_instances(await fresh_database())
        assert [r for r in schema if isinstance(r, Exception)] == []  # 没有人因为竞争而崩溃
        assert knowledge == [True] * STARTERS  # 也没有人被悄悄降级成"没有语义检索"
    assert "knowledge schema unavailable" not in caplog.text


async def test_running_it_again_on_an_existing_database_changes_nothing(fresh_database):
    url = await fresh_database()
    schema, knowledge = await start_instances(url)
    again_schema, again_knowledge = await start_instances(url)
    assert not [r for r in schema + again_schema if isinstance(r, Exception)]
    assert knowledge == again_knowledge == [True] * STARTERS


async def test_the_lock_is_released_when_the_transaction_ends_so_a_crash_cannot_block_startup(fresh_database):
    url = await fresh_database()
    pool = create_pool(url, max_size=2)
    await pool.open(wait=True, timeout=5)
    try:
        with pytest.raises(RuntimeError):
            async with pool.connection() as conn:  # 拿到锁之后"崩溃"：事务回滚
                await db._schema_lock(conn)
                raise RuntimeError("crash while holding the lock")
        await asyncio.wait_for(init_schema(pool), timeout=5)  # 锁已经随事务释放，不会永远卡住
    finally:
        await pool.close()
