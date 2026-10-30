"""语义检索的集成测试（需要带 pgvector 的 PostgreSQL）。

这里用离线的 HashEmbedder 入库：它只反映【用词重叠】，所以这些测试验证的是检索的"管道"是否正确
（排序、过滤、阈值、模型一致性检查、索引是否被用上），而不是语义质量。
真正的语义质量要用 Gemini 向量化的结果来评估（见 Phase 5.6 的评估脚本）。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from psycopg.conninfo import make_conninfo

from app.config import Settings
from app.rag.chunking import chunk_document
from app.rag.documents import load_documents
from app.rag.embedder import HashEmbedder
from app.rag.ingest import ingest, to_vector_literal
from app.rag.retrieval import SEARCH_SQL, KnowledgeBase, KnowledgeIndexMismatch


@pytest.fixture
async def kb(knowledge_pool):
    """已经用 HashEmbedder 把真实语料入库的知识库。"""
    embedder = HashEmbedder()
    await ingest(knowledge_pool, embedder, load_documents())
    return KnowledgeBase(knowledge_pool, embedder)


POOL_QUERY = "HikariPool connection is not available, request timed out, connection pool exhausted"


# ------------------------------------------------------------ 基本行为
async def test_the_most_relevant_chunks_come_first_in_descending_score_order(kb):
    hits = await kb.search(POOL_QUERY, k=5)

    assert len(hits) == 5
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(-1.0 <= s <= 1.0 for s in scores)
    # 连接池相关的文档应当占据最前面：RB-101（runbook）和 INC-2031（相似的历史事故）。
    assert {hits[0].doc_id, hits[1].doc_id} <= {"RB-101", "INC-2031"}


async def test_hits_carry_everything_needed_to_cite_the_source(kb):
    [hit] = await kb.search(POOL_QUERY, k=1)
    assert hit.doc_id and hit.title and hit.doc_type in ("runbook", "incident")
    assert hit.content and hit.chunk_index >= 0


async def test_every_chunk_is_found_by_its_own_text(kb, knowledge_pool):
    # 自检（self-retrieval）：用一个块自己的文字去查，它必须排第一，相似度约为 1。
    # 这是对整条链路（切块 -> 向量化 -> 入库 -> 检索）最直接的端到端检查：任何一环出错，这里都会暴露。
    misses = []
    for document in load_documents():
        for chunk in chunk_document(document):
            [top] = await kb.search(chunk.embedding_text, k=1)
            if (top.doc_id, top.chunk_index) != (document.doc_id, chunk.index) or top.score < 0.999:
                misses.append((document.doc_id, chunk.index, top.doc_id, top.chunk_index, round(top.score, 3)))
    assert misses == []


async def test_k_limits_the_number_of_results(kb):
    assert len(await kb.search(POOL_QUERY, k=2)) == 2
    assert len(await kb.search(POOL_QUERY, k=20)) == 20


@pytest.mark.parametrize("k", [0, -1, 21])
async def test_k_must_be_in_range(kb, k):
    with pytest.raises(ValueError):
        await kb.search(POOL_QUERY, k=k)


async def test_an_empty_query_is_rejected(kb):
    with pytest.raises(ValueError):
        await kb.search("   ")


# ------------------------------------------------------------ 过滤
async def test_document_types_can_be_filtered(kb):
    hits = await kb.search(POOL_QUERY, k=10, doc_types=["incident"])
    assert hits and {h.doc_type for h in hits} == {"incident"}


async def test_services_filter_keeps_applicable_and_generic_documents_only(kb):
    hits = await kb.search(POOL_QUERY, k=20, services=["search-service"])
    ids = {h.doc_id for h in hits}

    # RB-101 只适用于 checkout/orders/payments，不适用于 search-service，必须被排除；
    assert "RB-101" not in ids and "INC-2031" not in ids
    # 不限服务的通用文档（services 为空）仍然应当出现。
    assert {"RB-007", "RB-140"} <= ids
    # 明确适用于 search-service 的文档也在。
    assert "RB-120" in ids


async def test_filters_that_match_nothing_return_an_empty_list_not_an_error(kb):
    hits = await kb.search(POOL_QUERY, services=["no-such-service"], doc_types=["incident"])
    # 没有叫这个名字的服务，但通用的事故复盘不存在（事故都有明确的服务）-> 空列表。
    assert hits == []


# ------------------------------------------------------------ 筛选
async def test_min_score_drops_weak_matches(kb):
    everything = await kb.search(POOL_QUERY, k=20)
    threshold = everything[4].score  # 第 5 名的分数
    strong = await kb.search(POOL_QUERY, k=20, min_score=threshold)

    assert 0 < len(strong) < len(everything)
    assert all(h.score >= threshold for h in strong)


async def test_max_per_doc_spreads_results_across_documents(kb):
    crowded = await kb.search(POOL_QUERY, k=8)
    spread = await kb.search(POOL_QUERY, k=8, max_per_doc=1)

    ids = [h.doc_id for h in spread]
    assert len(ids) == len(set(ids))  # 每篇文档只出现一次
    assert len(set(h.doc_id for h in spread)) >= len(set(h.doc_id for h in crowded))


# ------------------------------------------------------------ 模型一致性
async def test_querying_with_a_different_model_than_the_index_is_an_explicit_error(kb, knowledge_pool):
    class OtherModel(HashEmbedder):
        @property
        def id(self) -> str:
            return "some-other-model:768"

    # 知识库是用 hash:768 建的，现在换了模型来查询：必须明确报错，而不是悄悄返回"什么都没找到"
    # 或者更糟的——返回看起来合理、实际毫无意义的结果。
    with pytest.raises(KnowledgeIndexMismatch, match="re-run scripts/ingest_knowledge.py"):
        await KnowledgeBase(knowledge_pool, OtherModel()).search(POOL_QUERY)


async def test_an_empty_knowledge_base_returns_no_results(knowledge_pool):
    assert await KnowledgeBase(knowledge_pool, HashEmbedder()).search(POOL_QUERY) == []


# ------------------------------------------------------------ 索引
async def test_the_search_query_can_use_the_hnsw_index(kb, knowledge_pool):
    # 禁用全表扫描，看查询计划是不是走了我们建的 HNSW 索引。
    # 如果 SQL 里的运算符和索引的运算符类不一致（比如用了 <-> 而索引是 vector_cosine_ops），
    # 索引就用不上，会悄悄退化成全表扫描。语料小的时候看不出来，大了就慢。
    vector = to_vector_literal(await HashEmbedder().embed_query(POOL_QUERY))
    params = {"query": vector, "embedder_id": "hash:768", "doc_types": None, "services": None, "candidates": 20}
    async with knowledge_pool.connection() as conn:
        await conn.execute("SET LOCAL enable_seqscan = off")
        plan = await (await conn.execute("EXPLAIN " + SEARCH_SQL, params)).fetchall()
    assert "idx_knowledge_chunks_embedding" in "\n".join(row[0] for row in plan)


async def test_using_the_wrong_distance_operator_would_not_use_the_index(kb, knowledge_pool):
    # 反例：证明上面的测试是有区分力的。用 <->（欧氏距离）时，索引确实用不上。
    vector = to_vector_literal(await HashEmbedder().embed_query(POOL_QUERY))
    async with knowledge_pool.connection() as conn:
        await conn.execute("SET LOCAL enable_seqscan = off")
        plan = await (
            await conn.execute(
                "EXPLAIN SELECT 1 FROM knowledge_chunks ORDER BY embedding <-> %s::vector LIMIT 5", (vector,)
            )
        ).fetchall()
    assert "idx_knowledge_chunks_embedding" not in "\n".join(row[0] for row in plan)


# ------------------------------------------------------------ 命令行脚本（像用户那样运行）
SEARCH_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "search_knowledge.py"


def run_search(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "GEMINI_API_KEY")}
    env["DATABASE_URL"] = make_conninfo(Settings(_env_file=None).database_url, dbname="agent_test")
    return subprocess.run(
        [sys.executable, str(SEARCH_SCRIPT), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120
    )


async def test_the_search_script_prints_ranked_results(kb, tmp_path):
    result = run_search(tmp_path, POOL_QUERY, "-k", "3", "--embedder", "hash")

    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line for line in result.stdout.splitlines() if line[:2] in ("1.", "2.", "3.")]
    assert len(lines) == 3
    assert "RB-101" in result.stdout or "INC-2031" in result.stdout


async def test_the_search_script_applies_filters(kb, tmp_path):
    result = run_search(tmp_path, POOL_QUERY, "-k", "8", "--type", "incident", "--embedder", "hash")
    assert result.returncode == 0
    assert "RB-" not in result.stdout  # 只剩事故复盘，没有 runbook


def test_the_search_script_rejects_an_invalid_k(knowledge_pool, tmp_path):
    result = run_search(tmp_path, "anything", "-k", "99", "--embedder", "hash")
    assert result.returncode == 2 and "k must be between" in result.stdout


def test_the_search_script_without_an_api_key_explains_how_to_proceed(knowledge_pool, tmp_path):
    result = run_search(tmp_path, "anything", "--embedder", "gemini")
    assert result.returncode == 2 and "EMBEDDING_PROVIDER=hash" in result.stdout
