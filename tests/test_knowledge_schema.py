"""知识库表结构的集成测试：需要带 pgvector 的 PostgreSQL，没有时自动跳过。

重点是验证数据库层面的保证：向量维度、级联删除、唯一约束、以及"按余弦距离取最近"真的有效。
"""

import psycopg
import pytest

from app.db import init_knowledge_schema


def vec(*leading: float, size: int = 768) -> str:
    """构造 768 维向量的文本形式 '[x,y,0,0,...]'。pgvector 接受这种文本写法。"""
    values = list(leading) + [0.0] * (size - len(leading))
    return "[" + ",".join(str(v) for v in values) + "]"


async def add_document(pool, doc_id="RB-1", doc_type="runbook", services=(), embedder_id="test"):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO knowledge_documents (doc_id, doc_type, title, services, content_hash, embedder_id) "
            "VALUES (%s, %s, %s, %s, 'h', %s)",
            (doc_id, doc_type, f"Title {doc_id}", list(services), embedder_id),
        )


async def add_chunk(pool, doc_id, index, embedding):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO knowledge_chunks (doc_id, chunk_index, heading, content, embedding) "
            "VALUES (%s, %s, 'h', 'c', %s::vector)",
            (doc_id, index, embedding),
        )


async def test_schema_creation_is_idempotent(knowledge_pool):
    assert await init_knowledge_schema(knowledge_pool) is True
    assert await init_knowledge_schema(knowledge_pool) is True


async def test_the_vector_index_is_hnsw_with_cosine_ops(knowledge_pool):
    async with knowledge_pool.connection() as conn:
        row = await (
            await conn.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_knowledge_chunks_embedding'")
        ).fetchone()
    assert "hnsw" in row[0] and "vector_cosine_ops" in row[0]


async def test_embeddings_of_the_wrong_dimension_are_rejected_by_the_database(knowledge_pool):
    await add_document(knowledge_pool)
    # 维度写死在列类型 vector(768) 里：存 3 维的向量会被直接拒绝，不会悄悄存进一批坏数据。
    with pytest.raises(psycopg.Error, match="expected 768 dimensions"):
        await add_chunk(knowledge_pool, "RB-1", 0, "[1,2,3]")


async def test_deleting_a_document_deletes_its_chunks(knowledge_pool):
    await add_document(knowledge_pool)
    await add_chunk(knowledge_pool, "RB-1", 0, vec(1))
    await add_chunk(knowledge_pool, "RB-1", 1, vec(0, 1))

    async with knowledge_pool.connection() as conn:
        await conn.execute("DELETE FROM knowledge_documents WHERE doc_id = 'RB-1'")
        count = (await (await conn.execute("SELECT count(*) FROM knowledge_chunks")).fetchone())[0]
    assert count == 0  # ON DELETE CASCADE：没有留下孤儿块


async def test_a_chunk_cannot_exist_without_its_document(knowledge_pool):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await add_chunk(knowledge_pool, "RB-404", 0, vec(1))


async def test_chunk_positions_are_unique_per_document(knowledge_pool):
    await add_document(knowledge_pool)
    await add_chunk(knowledge_pool, "RB-1", 0, vec(1))
    with pytest.raises(psycopg.errors.UniqueViolation):
        await add_chunk(knowledge_pool, "RB-1", 0, vec(0, 1))


async def test_document_type_is_constrained(knowledge_pool):
    with pytest.raises(psycopg.errors.CheckViolation):
        await add_document(knowledge_pool, doc_type="memo")


async def test_nearest_neighbour_by_cosine_distance_orders_correctly(knowledge_pool):
    await add_document(knowledge_pool, "RB-1")
    await add_chunk(knowledge_pool, "RB-1", 0, vec(1, 0))  # 方向和查询完全一致
    await add_chunk(knowledge_pool, "RB-1", 1, vec(1, 1))  # 夹角 45 度
    await add_chunk(knowledge_pool, "RB-1", 2, vec(0, 1))  # 垂直
    await add_chunk(knowledge_pool, "RB-1", 3, vec(-1, 0))  # 方向相反

    async with knowledge_pool.connection() as conn:
        rows = await (
            await conn.execute(
                # <=> 是余弦距离运算符；1 - 距离 = 余弦相似度。
                "SELECT chunk_index, 1 - (embedding <=> %s::vector) AS similarity "
                "FROM knowledge_chunks ORDER BY embedding <=> %s::vector",
                (vec(1, 0), vec(1, 0)),
            )
        ).fetchall()

    assert [r[0] for r in rows] == [0, 1, 2, 3]
    assert [round(r[1], 3) for r in rows] == [1.0, 0.707, 0.0, -1.0]


async def test_cosine_distance_ignores_vector_length(knowledge_pool):
    await add_document(knowledge_pool)
    await add_chunk(knowledge_pool, "RB-1", 0, vec(5, 0))  # 很长，但方向和查询相同
    await add_chunk(knowledge_pool, "RB-1", 1, vec(0.1, 0.1))  # 很短，方向偏 45 度

    async with knowledge_pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT chunk_index FROM knowledge_chunks ORDER BY embedding <=> %s::vector", (vec(1, 0),)
            )
        ).fetchall()
    assert [r[0] for r in rows] == [0, 1]


async def test_services_can_be_filtered_with_array_overlap(knowledge_pool):
    await add_document(knowledge_pool, "RB-1", services=["checkout-api", "orders-api"])
    await add_document(knowledge_pool, "RB-2", services=["search-service"])
    await add_document(knowledge_pool, "RB-3", services=[])  # 空 = 适用于所有服务

    async with knowledge_pool.connection() as conn:
        rows = await (
            await conn.execute(
                # && 是数组"有交集"运算符；空数组表示通用文档，对任何服务都适用。
                "SELECT doc_id FROM knowledge_documents WHERE services = '{}' OR services && %s::text[] ORDER BY doc_id",
                (["checkout-api"],),
            )
        ).fetchall()
    assert [r[0] for r in rows] == ["RB-1", "RB-3"]
