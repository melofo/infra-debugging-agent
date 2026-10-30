"""入库流水线的集成测试（需要带 pgvector 的 PostgreSQL，没有时自动跳过）。

核心要验证的性质：
  幂等    重复运行不改变结果，也不重复调用（收费的）向量化接口
  增量    只处理变了的文档；删掉的文档会被清除；换了模型或切块参数会全部重做
  原子    任何一步失败，数据库保持原样，不会留下"半个知识库"
  并发    两个入库任务同时运行，结果仍然一致
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from psycopg.conninfo import make_conninfo

from app.config import Settings
from app.rag.documents import load_documents
from app.rag.embedder import EmbeddingError, HashEmbedder
from app.rag.ingest import IngestReport, ingest, to_vector_literal

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ingest_knowledge.py"


class CountingEmbedder(HashEmbedder):
    """记录向量化调用的次数和文本数量，用来证明"没变的文档没有被重新向量化"。"""

    def __init__(self, embedder_id="hash:768", fail=False, bad_dimension_marker=None):
        super().__init__()
        self._id = embedder_id
        self.fail = fail
        self.bad_dimension_marker = bad_dimension_marker
        self.calls: list[int] = []

    @property
    def id(self) -> str:
        return self._id

    async def embed_documents(self, texts):
        self.calls.append(len(texts))
        if self.fail:
            raise EmbeddingError("embedding service unavailable")
        vectors = await super().embed_documents(texts)
        if self.bad_dimension_marker:
            # 对含有标记的文本返回维度不对的向量：向量化"成功"了，但写库时数据库会拒绝。
            vectors = [[0.5, 0.5, 0.5] if self.bad_dimension_marker in t else v for t, v in zip(texts, vectors)]
        return vectors


def write_doc(directory: Path, doc_id: str, body: str, title: str = "Title") -> None:
    (directory / f"{doc_id.lower()}.md").write_text(
        f"---\nid: {doc_id}\ntype: runbook\ntitle: {title} {doc_id}\nservices: [checkout-api]\ntags: [demo]\n---\n"
        f"# {title}\n\n## Symptoms\n\n{body}\n\n## Mitigation\n\nFix {doc_id} by restarting.\n"
    )


@pytest.fixture
def corpus(tmp_path):
    write_doc(tmp_path, "RB-901", "alpha symptoms of the first problem")
    write_doc(tmp_path, "RB-902", "bravo symptoms of the second problem")
    write_doc(tmp_path, "RB-903", "charlie symptoms of the third problem")
    return tmp_path


async def one(pool, sql, params=()):
    async with pool.connection() as conn:
        return (await (await conn.execute(sql, params)).fetchone())[0]


async def rows(pool, sql, params=()):
    async with pool.connection() as conn:
        return await (await conn.execute(sql, params)).fetchall()


# ------------------------------------------------------------ 首次入库
async def test_first_ingest_stores_every_document_and_chunk(knowledge_pool):
    documents = load_documents()
    embedder = CountingEmbedder()

    report = await ingest(knowledge_pool, embedder, documents)

    assert len(report.added) == 13 and report.chunks_embedded == 49
    assert await one(knowledge_pool, "SELECT count(*) FROM knowledge_documents") == 13
    assert await one(knowledge_pool, "SELECT count(*) FROM knowledge_chunks") == 49
    assert embedder.calls == [49]  # 所有块一次性交给向量化器（它内部再分批）
    # 存下了向量化器标识和切块参数，以后据此判断要不要重做。
    assert await one(knowledge_pool, "SELECT DISTINCT embedder_id FROM knowledge_documents") == "hash:768"
    assert await one(knowledge_pool, "SELECT DISTINCT chunking FROM knowledge_documents") == "max=1000,overlap=120"
    assert await one(knowledge_pool, "SELECT count(*) FROM knowledge_chunks WHERE vector_dims(embedding) <> 768") == 0


async def test_the_stored_vector_is_the_one_the_embedder_produced(knowledge_pool, corpus):
    documents = load_documents(corpus)
    await ingest(knowledge_pool, HashEmbedder(), documents)

    stored = await rows(knowledge_pool, "SELECT content FROM knowledge_chunks WHERE doc_id = 'RB-901' ORDER BY chunk_index")
    text = f"Title RB-901 (RB-901) - Symptoms\n\n{stored[0][0]}"
    expected = to_vector_literal(await HashEmbedder().embed_query(text))
    # 把期望的向量和库里的向量做余弦距离比较：同一个向量，距离应当是 0。
    distance = await one(
        knowledge_pool,
        "SELECT embedding <=> %s::vector FROM knowledge_chunks WHERE doc_id = 'RB-901' AND chunk_index = 0",
        (expected,),
    )
    assert distance == pytest.approx(0.0, abs=1e-6)


# ------------------------------------------------------------ 幂等与增量
async def test_running_again_changes_nothing_and_does_not_call_the_embedder(knowledge_pool, corpus):
    documents = load_documents(corpus)
    await ingest(knowledge_pool, CountingEmbedder(), documents)
    ids_before = await rows(knowledge_pool, "SELECT id FROM knowledge_chunks ORDER BY id")

    embedder = CountingEmbedder()
    report = await ingest(knowledge_pool, embedder, documents)

    assert sorted(report.unchanged) == ["RB-901", "RB-902", "RB-903"] and not report.changed
    assert embedder.calls == []  # 没变的文档不重新向量化：向量化接口是按用量收费的
    assert await rows(knowledge_pool, "SELECT id FROM knowledge_chunks ORDER BY id") == ids_before  # 连行都没被动过


async def test_only_the_changed_document_is_reprocessed(knowledge_pool, corpus):
    await ingest(knowledge_pool, CountingEmbedder(), load_documents(corpus))
    other_ids = await rows(knowledge_pool, "SELECT id FROM knowledge_chunks WHERE doc_id <> 'RB-902' ORDER BY id")

    write_doc(corpus, "RB-902", "bravo symptoms rewritten completely differently")
    embedder = CountingEmbedder()
    report = await ingest(knowledge_pool, embedder, load_documents(corpus))

    assert report.updated == ["RB-902"] and sorted(report.unchanged) == ["RB-901", "RB-903"]
    assert embedder.calls == [2]  # 只有这一篇的 2 个块被重新向量化
    contents = [r[0] for r in await rows(knowledge_pool, "SELECT content FROM knowledge_chunks WHERE doc_id = 'RB-902'")]
    assert any("rewritten completely" in c for c in contents)
    assert not any("second problem" in c for c in contents)  # 旧内容没有残留
    assert await rows(knowledge_pool, "SELECT id FROM knowledge_chunks WHERE doc_id <> 'RB-902' ORDER BY id") == other_ids


async def test_documents_removed_from_the_corpus_are_deleted_with_their_chunks(knowledge_pool, corpus):
    await ingest(knowledge_pool, CountingEmbedder(), load_documents(corpus))
    (corpus / "rb-903.md").unlink()

    report = await ingest(knowledge_pool, CountingEmbedder(), load_documents(corpus))

    assert report.deleted == ["RB-903"]
    assert await one(knowledge_pool, "SELECT count(*) FROM knowledge_documents WHERE doc_id = 'RB-903'") == 0
    assert await one(knowledge_pool, "SELECT count(*) FROM knowledge_chunks WHERE doc_id = 'RB-903'") == 0


async def test_deletion_can_be_switched_off(knowledge_pool, corpus):
    await ingest(knowledge_pool, CountingEmbedder(), load_documents(corpus))
    (corpus / "rb-903.md").unlink()

    report = await ingest(knowledge_pool, CountingEmbedder(), load_documents(corpus), delete_missing=False)

    assert report.deleted == []
    assert await one(knowledge_pool, "SELECT count(*) FROM knowledge_documents") == 3


async def test_a_different_embedding_model_forces_a_full_reindex(knowledge_pool, corpus):
    documents = load_documents(corpus)
    await ingest(knowledge_pool, CountingEmbedder("model-a"), documents)

    embedder = CountingEmbedder("model-b")
    report = await ingest(knowledge_pool, embedder, documents)

    # 不同模型的向量不在同一个空间，混着用检索结果没有意义，所以必须全部重做。
    assert sorted(report.updated) == ["RB-901", "RB-902", "RB-903"]
    assert await one(knowledge_pool, "SELECT DISTINCT embedder_id FROM knowledge_documents") == "model-b"


async def test_different_chunking_parameters_force_a_reindex(knowledge_pool, corpus):
    documents = load_documents(corpus)
    await ingest(knowledge_pool, CountingEmbedder(), documents)

    report = await ingest(knowledge_pool, CountingEmbedder(), documents, max_chars=500)

    assert len(report.updated) == 3
    assert await one(knowledge_pool, "SELECT DISTINCT chunking FROM knowledge_documents") == "max=500,overlap=120"


async def test_a_dry_run_reports_the_plan_without_touching_anything(knowledge_pool, corpus):
    embedder = CountingEmbedder()
    report = await ingest(knowledge_pool, embedder, load_documents(corpus), dry_run=True)

    assert sorted(report.added) == ["RB-901", "RB-902", "RB-903"] and report.chunks_embedded == 6
    assert report.dry_run and "[dry run]" in report.summary()
    assert embedder.calls == []  # 没有调用接口
    assert await one(knowledge_pool, "SELECT count(*) FROM knowledge_documents") == 0  # 没有写库


# ------------------------------------------------------------ 原子性
async def test_if_embedding_fails_the_database_is_left_exactly_as_it_was(knowledge_pool, corpus):
    await ingest(knowledge_pool, CountingEmbedder(), load_documents(corpus))
    before = await rows(knowledge_pool, "SELECT doc_id, content_hash FROM knowledge_documents ORDER BY doc_id")

    write_doc(corpus, "RB-901", "alpha rewritten")
    write_doc(corpus, "RB-904", "a brand new document")
    (corpus / "rb-903.md").unlink()
    with pytest.raises(EmbeddingError):
        await ingest(knowledge_pool, CountingEmbedder(fail=True), load_documents(corpus))

    # 新增、更新、删除一个都没有发生。
    assert await rows(knowledge_pool, "SELECT doc_id, content_hash FROM knowledge_documents ORDER BY doc_id") == before


async def test_a_failure_in_the_middle_of_the_write_rolls_everything_back(knowledge_pool, corpus):
    await ingest(knowledge_pool, CountingEmbedder(), load_documents(corpus))
    old_chunks = await rows(knowledge_pool, "SELECT doc_id, content FROM knowledge_chunks ORDER BY id")

    # RB-901 的更新本身没问题（先被处理）；RB-903 的向量维度是错的，写库时数据库会拒绝它（后被处理）。
    write_doc(corpus, "RB-901", "alpha rewritten so it should be updated")
    write_doc(corpus, "RB-903", "charlie with a FAULTY-MARKER inside")
    with pytest.raises(Exception, match="expected 768 dimensions"):
        await ingest(knowledge_pool, CountingEmbedder(bad_dimension_marker="FAULTY-MARKER"), load_documents(corpus))

    # 关键：RB-901 的更新也被撤销了。没有"一半新、一半旧"。
    assert await rows(knowledge_pool, "SELECT doc_id, content FROM knowledge_chunks ORDER BY id") == old_chunks


# ------------------------------------------------------------ 并发
async def test_two_ingests_running_at_the_same_time_leave_a_consistent_result(knowledge_pool):
    documents = load_documents()

    await asyncio.gather(
        ingest(knowledge_pool, CountingEmbedder(), documents),
        ingest(knowledge_pool, CountingEmbedder(), documents),
    )

    # 没有因为互相抢着写而产生重复、缺失，也没有抛出唯一约束冲突。
    # （注意：这个场景里两个任务按同样的顺序处理同一批文档，数据库的行锁已经让它们自然排队，
    #  所以它【并不证明建议锁有用】；建议锁的价值由下面"相反顺序"的测试证明。）
    assert await one(knowledge_pool, "SELECT count(*) FROM knowledge_documents") == 13
    assert await one(knowledge_pool, "SELECT count(*) FROM knowledge_chunks") == 49
    assert await one(knowledge_pool, "SELECT count(*) FROM (SELECT doc_id, chunk_index FROM knowledge_chunks GROUP BY 1, 2 HAVING count(*) > 1) d") == 0


async def test_runs_that_process_the_same_documents_in_opposite_orders_do_not_deadlock(knowledge_pool):
    # 这才是建议锁真正保护的场景。上面的测试里两个任务按同样的顺序处理文档，
    # 数据库的行锁已经让它们自然排队，建议锁用不上（变异检查证明了这一点）。
    # 这里让一个任务正序、另一个倒序：A 锁住文档 1 再要文档 2，B 锁住文档 2 再要文档 1，互相等待 = 死锁。
    # 没有建议锁的话，数据库会检测到死锁并让其中一个任务失败。
    documents = load_documents()

    await asyncio.gather(
        ingest(knowledge_pool, CountingEmbedder(), documents),
        ingest(knowledge_pool, CountingEmbedder(), list(reversed(documents))),
    )

    assert await one(knowledge_pool, "SELECT count(*) FROM knowledge_documents") == 13
    assert await one(knowledge_pool, "SELECT count(*) FROM knowledge_chunks") == 49


# ------------------------------------------------------------ 小工具
def test_vector_literal_format():
    assert to_vector_literal([0.5, -1.0, 0.0]) == "[0.5,-1,0]"
    assert to_vector_literal([1 / 3]) == "[0.33333333]"


def test_empty_report_summary():
    assert IngestReport().summary() == "added 0, updated 0, deleted 0, unchanged 0; 0 chunks embedded"


# ------------------------------------------------------------ 命令行脚本（像用户那样运行）
def run_cli(cwd: Path, *args: str, **env) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "GEMINI_API_KEY")}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=cwd, env={**base, **env}, capture_output=True, text=True, timeout=120
    )


@pytest.fixture
def cli_env(knowledge_pool):
    base_url = Settings(_env_file=None).database_url
    return {"DATABASE_URL": make_conninfo(base_url, dbname="agent_test"), "WORKER_ENABLED": "false"}


async def test_cli_ingests_then_skips_unchanged_documents_on_the_second_run(knowledge_pool, cli_env, tmp_path):
    first = run_cli(tmp_path, "--embedder", "hash", **cli_env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "added 13" in first.stdout and "49 chunks embedded" in first.stdout
    assert await one(knowledge_pool, "SELECT count(*) FROM knowledge_chunks") == 49

    second = run_cli(tmp_path, "--embedder", "hash", **cli_env)
    assert second.returncode == 0
    assert "unchanged 13" in second.stdout and "0 chunks embedded" in second.stdout


async def test_cli_dry_run_writes_nothing(knowledge_pool, cli_env, tmp_path):
    result = run_cli(tmp_path, "--embedder", "hash", "--dry-run", **cli_env)
    assert result.returncode == 0 and "[dry run]" in result.stdout
    assert await one(knowledge_pool, "SELECT count(*) FROM knowledge_documents") == 0


def test_cli_reports_an_invalid_document_with_its_file_name(knowledge_pool, cli_env, tmp_path):
    (tmp_path / "broken.md").write_text("no front matter here")
    result = run_cli(tmp_path, "--embedder", "hash", "--dir", str(tmp_path), **cli_env)
    assert result.returncode == 2 and "broken.md" in result.stdout


def test_cli_without_an_api_key_explains_how_to_proceed(knowledge_pool, cli_env, tmp_path):
    result = run_cli(tmp_path, "--embedder", "gemini", **cli_env)
    assert result.returncode == 2
    assert "GEMINI_API_KEY" in result.stdout and "EMBEDDING_PROVIDER=hash" in result.stdout
