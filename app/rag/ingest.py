"""入库流水线：把知识库文档 切块 -> 向量化 -> 写进 PostgreSQL（pgvector）。

【设计目标】运行多少次、在什么状态下运行，结果都一样（幂等），并且不会留下"半个知识库"。

    语料目录 ---读取---> 和数据库里已有的对比 ---> 分成四类：
        新增 (added)       数据库里没有
        更新 (updated)     内容变了，或者换了向量化模型 / 切块参数
        不变 (unchanged)   什么都不用做 —— 不调用向量化接口，不花钱
        删除 (deleted)     数据库里有，但语料里已经没了

    只对"新增 + 更新"的文档做：切块 -> 向量化 -> 写库。

【最关键的设计：向量化在事务【外】，写库在一个事务【内】】
向量化要调外部接口，可能慢、可能限流、可能中途失败。如果边向量化边写库，
失败时数据库里就会留着一半新、一半旧的内容，检索结果不可预测。
所以先把所有要写的向量在内存里全部算好（这一步失败，数据库完全没动过），
然后在【同一个事务】里一次性切换：要么全部生效，要么全部不生效。

    代价：一次入库的所有内容必须放得进内存、放得进一个事务。对几十篇文档没问题；
    几十万篇文档的话要改成分批、每批一个事务，并且要处理"部分完成"的状态。
"""

import logging
from dataclasses import dataclass, field

from psycopg_pool import AsyncConnectionPool

from app.rag.chunking import DEFAULT_MAX_CHARS, DEFAULT_OVERLAP_CHARS, Chunk, chunk_document
from app.rag.documents import Document
from app.rag.embedder import Embedder

logger = logging.getLogger(__name__)

# 建议锁（advisory lock）的编号：随便选的一个固定数字，只要所有入库进程用同一个就行。
# 它不锁任何表或行，只是一个"名字"，同一时刻只有一个事务能持有它。
INGEST_LOCK_KEY = 7_305_001


@dataclass
class IngestReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    chunks_embedded: int = 0
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.deleted)

    def summary(self) -> str:
        prefix = "[dry run] would have " if self.dry_run else ""
        return (
            f"{prefix}added {len(self.added)}, updated {len(self.updated)}, deleted {len(self.deleted)}, "
            f"unchanged {len(self.unchanged)}; {self.chunks_embedded} chunks embedded"
        )


def to_vector_literal(vector: list[float]) -> str:
    """把 Python 列表转成 pgvector 接受的文本形式 '[0.1,0.2,...]'，写入时再用 ::vector 转换类型。

    pgvector 内部用 32 位浮点数存储，所以保留 8 位有效数字就足够了。
    """
    return "[" + ",".join(format(x, ".8g") for x in vector) + "]"


async def ingest(
    pool: AsyncConnectionPool,
    embedder: Embedder,
    documents: list[Document],
    *,
    delete_missing: bool = True,
    dry_run: bool = False,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> IngestReport:
    report = IngestReport(dry_run=dry_run)
    chunking = f"max={max_chars},overlap={overlap_chars}"

    # ---- 1. 看数据库里现在有什么 -------------------------------------------------
    async with pool.connection() as conn:
        rows = await (
            await conn.execute("SELECT doc_id, content_hash, embedder_id, chunking FROM knowledge_documents")
        ).fetchall()
    existing = {doc_id: (content_hash, embedder_id, chunk_cfg) for doc_id, content_hash, embedder_id, chunk_cfg in rows}

    # ---- 2. 分类 ------------------------------------------------------------------
    to_index: list[Document] = []
    for document in documents:
        current = existing.get(document.doc_id)
        if current is None:
            report.added.append(document.doc_id)
        elif current != (document.content_hash, embedder.id, chunking):
            # 三个条件任何一个不同都要重做：内容变了；换了向量化模型（新旧向量不在同一个空间，不能混用）；
            # 切块参数变了（同一篇文档会切出不同的块）。
            report.updated.append(document.doc_id)
        else:
            report.unchanged.append(document.doc_id)
            continue
        to_index.append(document)
    if delete_missing:
        wanted = {d.doc_id for d in documents}
        report.deleted = sorted(set(existing) - wanted)

    # ---- 3. 切块 + 向量化（在事务之外；失败时数据库完全没动）---------------------------
    chunked = {d.doc_id: chunk_document(d, max_chars, overlap_chars) for d in to_index}
    texts = [chunk.embedding_text for chunks in chunked.values() for chunk in chunks]
    report.chunks_embedded = len(texts)
    if dry_run or not report.changed:
        return report

    # 所有文档的所有块放在一次调用里向量化（embedder 内部会按批次分开发送），比逐篇调用更省请求次数。
    vectors = await embedder.embed_documents(texts) if texts else []

    # 把扁平的向量列表按文档切回去。
    per_document: dict[str, list[tuple[Chunk, list[float]]]] = {}
    cursor = 0
    for doc_id, chunks in chunked.items():
        per_document[doc_id] = list(zip(chunks, vectors[cursor : cursor + len(chunks)]))
        cursor += len(chunks)

    # ---- 4. 在【一个事务】里切换 ----------------------------------------------------
    by_id = {d.doc_id: d for d in to_index}
    async with pool.connection() as conn:
        # `async with pool.connection()` 的整个块就是一个事务：正常结束自动提交，中途抛异常自动回滚。
        # 建议锁：两个入库任务同时运行时，后来的会在这里等前一个提交完。
        # pg_advisory_xact_lock 的锁在事务结束时自动释放，不会因为程序崩溃而遗留。
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (INGEST_LOCK_KEY,))

        for doc_id, pairs in per_document.items():
            doc = by_id[doc_id]
            # upsert：不存在就插入，存在就更新（ON CONFLICT ... DO UPDATE）。
            await conn.execute(
                """
                INSERT INTO knowledge_documents
                    (doc_id, doc_type, title, services, tags, content_hash, embedder_id, chunking, indexed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (doc_id) DO UPDATE SET
                    doc_type = EXCLUDED.doc_type, title = EXCLUDED.title, services = EXCLUDED.services,
                    tags = EXCLUDED.tags, content_hash = EXCLUDED.content_hash,
                    embedder_id = EXCLUDED.embedder_id, chunking = EXCLUDED.chunking, indexed_at = now()
                """,
                (
                    doc.doc_id, doc.doc_type, doc.title, list(doc.services), list(doc.tags),
                    doc.content_hash, embedder.id, chunking,
                ),
            )
            # 旧的块整体删掉再插入新的：不用逐块比较"哪些变了"，简单且不会残留过期的块。
            await conn.execute("DELETE FROM knowledge_chunks WHERE doc_id = %s", (doc_id,))
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO knowledge_chunks (doc_id, chunk_index, heading, content, embedding) "
                    "VALUES (%s, %s, %s, %s, %s::vector)",
                    [(doc_id, c.index, c.heading, c.content, to_vector_literal(v)) for c, v in pairs],
                )

        if report.deleted:
            # 块会被 ON DELETE CASCADE 自动一起删除。
            await conn.execute("DELETE FROM knowledge_documents WHERE doc_id = ANY(%s)", (report.deleted,))

    logger.info("knowledge ingested", extra={"added": len(report.added), "updated": len(report.updated),
                                             "deleted": len(report.deleted), "chunks": report.chunks_embedded})
    return report
