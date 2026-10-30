"""语义检索：给一个问题，找出知识库里最相关的文档块。

    问题 --embed_query--> 查询向量 --在 pgvector 里找最近的块--> 排好序的命中结果

【相似度】数据库的 `<=>` 运算符算的是余弦【距离】（0 = 方向完全一致，2 = 完全相反）。
我们对外返回余弦【相似度】= 1 - 距离（1 = 完全一致，0 = 无关，-1 = 相反），数值越大越相关，更符合直觉。

【三个重要的设计点】
1. 查询向量和库里的向量必须来自【同一个】向量化模型。模型不同，向量不在同一个空间里，
   比较出来的"相似度"毫无意义，而且不会报任何错。所以这里显式检查，不一致就明确报错。
2. 先让向量索引取出一批【候选】（比 k 多几倍），再筛选：元数据过滤（服务、文档类型）在 SQL 外层，
   相似度阈值和"每篇文档最多几块"在应用里。原因见下面 SEARCH_SQL 旁边的说明：带过滤的查询用不上向量索引。
3. 已知局限（后过滤）：先取候选再过滤，过滤条件很严格而候选里恰好没有符合的，可能返回不足 k 条。
   带过滤时我们把候选池放大到至少 FILTERED_MIN_CANDIDATES 条来缓解。我们的语料只有几十个块，
   候选池覆盖了全部，所以结果是精确的；语料很大时需要继续调大它，或使用 pgvector 的迭代扫描（hnsw.iterative_scan）。
"""

from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from app.rag.embedder import Embedder

MAX_K = 20
# 取候选的倍数：要 k 条结果，先取 k * CANDIDATE_FACTOR 个候选，筛选之后再截取前 k 个。
CANDIDATE_FACTOR = 4
# 带元数据过滤时，候选池至少这么大：过滤会丢掉一部分候选，池子大一些才不容易不够 k 条。
FILTERED_MIN_CANDIDATES = 100


class KnowledgeIndexMismatch(Exception):
    """知识库是用另一个向量化模型建的索引：查询向量和库里的向量没法比较，需要重新入库。"""


@dataclass(frozen=True)
class Hit:
    doc_id: str
    doc_type: str
    title: str
    chunk_index: int
    heading: str | None
    content: str
    score: float  # 余弦相似度，越大越相关


# 注意查询里用的是 <=>（余弦距离），和建索引时的 vector_cosine_ops 一致。
# 如果误写成 <->（欧氏距离）或 <#>（内积），数据库就用不上这个 HNSW 索引，退化成全表扫描，
# 结果也不是按余弦排序的，而且不会有任何报错。
#
# 【SQL 为什么写成"先取最近的候选，再过滤"】这是我用执行计划做实验得出的结论：
#   向量索引只能服务"纯按距离排序"的查询（ORDER BY 距离 LIMIT n，不带任何过滤）。
#   只要里层带了 WHERE 过滤（哪怕只是同一张表的普通列，或者子查询、JOIN），
#   规划器就不再走向量索引，改成"取出符合条件的行再排序"。
#   我先后写过两个版本（JOIN 文档表过滤 / 子查询过滤），执行计划检查都抓到了没走索引。
# 所以：里层是纯近邻搜索（走 HNSW 索引），外层再 JOIN 文档表、做元数据过滤。
# 这叫"后过滤"（post-filtering），代价是过滤很严格时可能返回不足 k 条，见 FILTERED_MIN_CANDIDATES。
SEARCH_SQL = """
WITH nearest AS (
    SELECT c.doc_id, c.chunk_index, c.heading, c.content, c.embedding <=> %(query)s::vector AS distance
    FROM knowledge_chunks c
    ORDER BY c.embedding <=> %(query)s::vector
    LIMIT %(candidates)s
)
SELECT n.doc_id, d.doc_type, d.title, n.chunk_index, n.heading, n.content, 1 - n.distance AS score
FROM nearest n
JOIN knowledge_documents d ON d.doc_id = n.doc_id
WHERE d.embedder_id = %(embedder_id)s
  AND (%(doc_types)s::text[] IS NULL OR d.doc_type = ANY(%(doc_types)s::text[]))
  -- 服务过滤：空数组表示"通用文档"，对任何服务都适用；否则要和要求的服务有交集（&& 是数组相交运算符）。
  AND (%(services)s::text[] IS NULL OR d.services = '{}' OR d.services && %(services)s::text[])
ORDER BY n.distance
"""


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(format(x, ".8g") for x in vector) + "]"


class KnowledgeBase:
    def __init__(self, pool: AsyncConnectionPool, embedder: Embedder):
        self._pool = pool
        self._embedder = embedder

    async def search(
        self,
        query: str,
        *,
        k: int = 5,
        doc_types: list[str] | None = None,
        services: list[str] | None = None,
        min_score: float | None = None,
        max_per_doc: int | None = None,
    ) -> list[Hit]:
        """返回最相关的至多 k 个块，按相似度从高到低。

        doc_types   只在这些类型里找（runbook / incident）
        services    只找适用于这些服务的文档（外加不限服务的通用文档）
        min_score   丢弃相似度低于这个值的结果。注意：合适的阈值取决于向量化模型，要用评估数据来定，不要凭感觉。
        max_per_doc 每篇文档最多返回几块，让结果覆盖更多不同的文档，而不是同一篇的各个小节占满名额。
        """
        if not query.strip():
            raise ValueError("query must not be empty")
        if not 1 <= k <= MAX_K:
            raise ValueError(f"k must be between 1 and {MAX_K}")

        has_filter = bool(doc_types or services)
        candidates = max(k * CANDIDATE_FACTOR, FILTERED_MIN_CANDIDATES) if has_filter else k * CANDIDATE_FACTOR
        query_vector = await self._embedder.embed_query(query)
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                SEARCH_SQL,
                {
                    "query": _vector_literal(query_vector),
                    "embedder_id": self._embedder.id,
                    "doc_types": doc_types or None,
                    "services": services or None,
                    "candidates": candidates,
                },
            )
            found = await cursor.fetchall()

            if not found:
                # 没有结果有两种可能：知识库是空的 / 过滤太严（正常）；或者知识库是用另一个模型建的（配置错误）。
                # 后者必须明确报错，否则调用方只会看到"什么都没找到"，很难想到是模型不一致。
                stale = (
                    await (
                        await conn.execute(
                            "SELECT DISTINCT embedder_id FROM knowledge_documents WHERE embedder_id <> %s",
                            (self._embedder.id,),
                        )
                    ).fetchall()
                )
                if stale:
                    raise KnowledgeIndexMismatch(
                        f"the knowledge base was indexed with {sorted(r[0] for r in stale)} but queries use "
                        f"{self._embedder.id!r}; re-run scripts/ingest_knowledge.py"
                    )

        hits: list[Hit] = []
        per_doc: dict[str, int] = {}
        for doc_id, doc_type, title, chunk_index, heading, content, score in found:
            if min_score is not None and score < min_score:
                continue  # 结果已按相似度降序，但这里不 break：逻辑简单，候选数也不多
            if max_per_doc is not None and per_doc.get(doc_id, 0) >= max_per_doc:
                continue
            per_doc[doc_id] = per_doc.get(doc_id, 0) + 1
            hits.append(Hit(doc_id, doc_type, title, chunk_index, heading, content, float(score)))
            if len(hits) == k:
                break
        return hits
