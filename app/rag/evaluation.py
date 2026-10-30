"""检索质量评估：用一批标注好的问题，比较几种检索方案谁更准。

    标注数据：每个问题标出"应该被找到的文档"        （data/eval/retrieval_queries.json）
    检索方案：给一个问题，返回按相关度排好序的文档编号
    指标：    排名里第一个正确答案出现得多靠前、前几名里找到了多少正确答案

【为什么评估在内存里做，不经过数据库】
这样同一个向量化器可以公平地比较所有方案，也方便调整块大小、尝试新方案，不用每次改数据库。
风险是：评估用的代码路径和线上真正用的（SQL）可能不一致，那评估结果就没有意义了。
所以 tests/test_rag_evaluation.py 里有一个一致性测试：同样的问题，内存版和数据库版必须给出同样的排序。

【指标的含义】（排名是"文档"层面的：一篇文档的多个块只算它排名最靠前的那一个）
  hit@k     前 k 名里有没有至少一个正确答案。回答"用户第一眼能不能看到有用的东西"
  recall@k  前 k 名里找到了所有正确答案的多少比例。回答"该找的有没有找全"
  MRR       第一个正确答案排名的倒数（第 1 名得 1，第 2 名得 0.5，第 3 名得 0.33……）的平均值。
            回答"平均来说，正确答案排得多靠前"，越接近 1 越好
"""

import hashlib
import json
import math
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app.rag.chunking import chunk_document
from app.rag.documents import Document
from app.rag.embedder import STOPWORDS, Embedder, normalize

EVAL_QUERIES_PATH = Path(__file__).resolve().parents[2] / "data" / "eval" / "retrieval_queries.json"
DEFAULT_KS = (1, 3, 5)


class EvalQuery(BaseModel):
    """disjoint 类别：故意和正确答案文档【几乎没有相同用词】的问题，专门检验语义检索。
    "几乎没有"由 tests/test_rag_evaluation.py 强制检查（词汇重叠度不超过 0.25），不是凭作者的感觉。"""

    id: str
    category: Literal["log-line", "paraphrase", "disjoint", "symptom", "look-alike", "how-to"]
    query: str = Field(min_length=3)
    relevant: list[str] = Field(min_length=1)  # 应该被找到的文档编号（只要排进前面就算找到，顺序不要求）


def load_queries(path: Path = EVAL_QUERIES_PATH) -> list[EvalQuery]:
    queries = [EvalQuery.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]
    ids = [q.id for q in queries]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate query ids in the evaluation set")
    return queries


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def first_relevant_rank(ranking: list[str], relevant: set[str]) -> int | None:
    """第一个正确答案的排名（从 1 开始）；排名里没有任何正确答案时返回 None。"""
    for position, doc_id in enumerate(ranking, start=1):
        if doc_id in relevant:
            return position
    return None


def hit_at(ranking: list[str], relevant: set[str], k: int) -> float:
    return 1.0 if any(doc_id in relevant for doc_id in ranking[:k]) else 0.0


def recall_at(ranking: list[str], relevant: set[str], k: int) -> float:
    return len(relevant & set(ranking[:k])) / len(relevant)


def reciprocal_rank(ranking: list[str], relevant: set[str]) -> float:
    rank = first_relevant_rank(ranking, relevant)
    return 0.0 if rank is None else 1.0 / rank


@dataclass(frozen=True)
class QueryResult:
    query: EvalQuery
    ranking: list[str]

    @property
    def relevant(self) -> set[str]:
        return set(self.query.relevant)

    @property
    def rank(self) -> int | None:
        return first_relevant_rank(self.ranking, self.relevant)


def aggregate(results: list[QueryResult], ks: tuple[int, ...] = DEFAULT_KS) -> dict[str, float]:
    """把每个问题的结果汇总成整体指标（对所有问题取平均）。"""
    n = len(results)
    summary: dict[str, float] = {}
    for k in ks:
        summary[f"hit@{k}"] = sum(hit_at(r.ranking, r.relevant, k) for r in results) / n
    for k in ks:
        if k > 1:
            summary[f"recall@{k}"] = sum(recall_at(r.ranking, r.relevant, k) for r in results) / n
    summary["mrr"] = sum(reciprocal_rank(r.ranking, r.relevant) for r in results) / n
    return summary


# ---------------------------------------------------------------------------
# 检索的"单元"：一段文字，属于某个文档
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Unit:
    doc_id: str
    text: str


def chunk_units(documents: list[Document], max_chars: int, overlap_chars: int) -> list[Unit]:
    """每个块一个单元：和线上入库时一样，用带标题头的 embedding_text。"""
    return [
        Unit(d.doc_id, chunk.embedding_text)
        for d in documents
        for chunk in chunk_document(d, max_chars, overlap_chars)
    ]


def whole_document_units(documents: list[Document]) -> list[Unit]:
    """每篇文档一个单元：整篇文字一个向量（用来检验"切块到底有没有必要"）。"""
    return [Unit(d.doc_id, f"{d.title} ({d.doc_id})\n\n{d.body}") for d in documents]


def rank_documents(units: list[Unit], scores: list[float]) -> list[str]:
    """按得分从高到低排序，同一篇文档只保留排名最靠前的那一次。得分相同按原顺序，保证结果可重复。"""
    order = sorted(range(len(units)), key=lambda i: (-scores[i], i))
    ranking: list[str] = []
    for i in order:
        if units[i].doc_id not in ranking:
            ranking.append(units[i].doc_id)
    return ranking


# ---------------------------------------------------------------------------
# 基线：随机排序
# ---------------------------------------------------------------------------
class RandomStrategy:
    """随机排序，作为"地板"：告诉你什么都不做能得多少分。

    评估里一定要有基线，否则一个数字没有参照：hit@3 = 0.5 听起来不错，
    但如果随机排序就能得 0.4，那 0.5 几乎没有意义。任何真正的方案都必须明显超过它。
    随机数种子由问题内容决定，所以每次运行结果相同（可重复）。
    """

    def __init__(self, name: str, doc_ids: list[str]):
        self.name = name
        self._doc_ids = list(dict.fromkeys(doc_ids))

    async def rank(self, query: str) -> list[str]:
        seed = int.from_bytes(hashlib.blake2b(query.encode(), digest_size=8).digest(), "big")
        ranking = list(self._doc_ids)
        random.Random(seed).shuffle(ranking)
        return ranking


# ---------------------------------------------------------------------------
# 方案 1：关键词检索（BM25）
# ---------------------------------------------------------------------------
def tokenize(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS]


class Bm25Index:
    """BM25：传统搜索引擎（Elasticsearch、Lucene）默认使用的关键词打分公式。

    直觉：一个词在这段文字里出现得越多越相关（但有上限，出现 10 次不等于重要 10 倍），
    一个词在所有文档里越稀有越有区分力（"the" 到处都是没价值，"HikariPool" 很少见就很有价值），
    越长的文字越容易偶然包含某个词，所以要按长度做修正。
    它只看【用词是否相同】，不理解意思：问题和文档用了不同的词，得分就是 0。
    """

    def __init__(self, texts: list[str], k1: float = 1.5, b: float = 0.75):
        self._k1, self._b = k1, b
        self._docs = [Counter(tokenize(t)) for t in texts]
        self._lengths = [sum(d.values()) for d in self._docs]
        self._avg_length = (sum(self._lengths) / len(self._docs)) or 1.0
        document_frequency = Counter(term for d in self._docs for term in d)
        n = len(self._docs)
        self._idf = {t: math.log(1 + (n - df + 0.5) / (df + 0.5)) for t, df in document_frequency.items()}

    def scores(self, query: str) -> list[float]:
        terms = set(tokenize(query))
        scores = []
        for counts, length in zip(self._docs, self._lengths):
            score = 0.0
            for term in terms:
                tf = counts.get(term, 0)
                if tf:
                    norm = tf + self._k1 * (1 - self._b + self._b * length / self._avg_length)
                    score += self._idf[term] * tf * (self._k1 + 1) / norm
            scores.append(score)
        return scores


class Strategy(Protocol):
    name: str

    async def rank(self, query: str) -> list[str]:
        """返回文档编号，按相关度从高到低。"""
        ...


class KeywordStrategy:
    def __init__(self, name: str, units: list[Unit]):
        self.name = name
        self._units = units
        self._index = Bm25Index([u.text for u in units])

    async def rank(self, query: str) -> list[str]:
        scores = self._index.scores(query)
        # 所有单元得分都是 0 说明问题里的词在语料里一个都没出现过：没有任何结果，而不是随便排个序假装有结果。
        if not any(scores):
            return []
        return rank_documents(self._units, scores)


# ---------------------------------------------------------------------------
# 方案 2：向量检索
# ---------------------------------------------------------------------------
class CachedEmbedder:
    """给向量化器加一层缓存：相同的文字只向量化一次。

    评估要比较好几种方案，它们用到的问题和很多文字是重复的。没有缓存的话，同样的文字会被反复发给
    向量化接口，白白花钱花时间。
    """

    def __init__(self, inner: Embedder):
        self._inner = inner
        self._documents: dict[str, list[float]] = {}
        self._queries: dict[str, list[float]] = {}
        self.calls = 0  # 实际发给底层向量化器的文字数量，用来衡量花了多少

    @property
    def id(self) -> str:
        return self._inner.id

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in dict.fromkeys(texts) if t not in self._documents]
        if missing:
            self.calls += len(missing)
            for text, vector in zip(missing, await self._inner.embed_documents(missing)):
                self._documents[text] = vector
        return [self._documents[t] for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        if text not in self._queries:
            self.calls += 1
            self._queries[text] = await self._inner.embed_query(text)
        return self._queries[text]


class VectorStrategy:
    def __init__(self, name: str, units: list[Unit], embedder: CachedEmbedder):
        self.name = name
        self._units = units
        self._embedder = embedder
        self._vectors: list[list[float]] | None = None

    async def rank(self, query: str) -> list[str]:
        if self._vectors is None:  # 第一次用到时才向量化所有单元
            self._vectors = [normalize(v) for v in await self._embedder.embed_documents([u.text for u in self._units])]
        q = normalize(await self._embedder.embed_query(query))
        # 两个都是单位向量，内积就等于余弦相似度。
        scores = [sum(a * b for a, b in zip(q, v)) for v in self._vectors]
        return rank_documents(self._units, scores)


# ---------------------------------------------------------------------------
# 方案 3：混合检索（用 RRF 融合多个排名）
# ---------------------------------------------------------------------------
class HybridStrategy:
    """倒数排名融合（Reciprocal Rank Fusion, RRF）：把几个检索方案的排名合并成一个。

    每个文档的得分 = 它在各个排名里 1 / (c + 名次) 之和。
    只用【名次】，不用原始得分：关键词的 BM25 得分和向量的余弦相似度不在同一个量纲上，没法直接相加，
    而名次是通用的。在好几个排名里都靠前的文档得分最高，只被一个方案排得很靠前的次之。
    常数 c（通常取 60）让名次靠后的差别不要被过度放大。
    """

    def __init__(self, name: str, strategies: list[Strategy], c: int = 60):
        self.name = name
        self._strategies = strategies
        self._c = c

    async def rank(self, query: str) -> list[str]:
        scores: dict[str, float] = defaultdict(float)
        first_seen: dict[str, int] = {}
        for strategy in self._strategies:
            for position, doc_id in enumerate(await strategy.rank(query), start=1):
                scores[doc_id] += 1.0 / (self._c + position)
                first_seen.setdefault(doc_id, len(first_seen))
        # 得分相同按首次出现的顺序，保证结果可重复。
        return sorted(scores, key=lambda d: (-scores[d], first_seen[d]))


# ---------------------------------------------------------------------------
# 把它们组合起来
# ---------------------------------------------------------------------------
@dataclass
class Evaluation:
    name: str
    results: list[QueryResult] = field(default_factory=list)

    def summary(self, ks: tuple[int, ...] = DEFAULT_KS) -> dict[str, float]:
        return aggregate(self.results, ks)

    def by_category(self, ks: tuple[int, ...] = DEFAULT_KS) -> dict[str, dict[str, float]]:
        grouped: dict[str, list[QueryResult]] = defaultdict(list)
        for result in self.results:
            grouped[result.query.category].append(result)
        return {category: aggregate(items, ks) for category, items in grouped.items()}


async def evaluate(strategy: Strategy, queries: list[EvalQuery]) -> Evaluation:
    return Evaluation(strategy.name, [QueryResult(q, await strategy.rank(q.query)) for q in queries])


def build_strategies(documents: list[Document], embedder: CachedEmbedder) -> list[Strategy]:
    chunks_1000 = chunk_units(documents, 1000, 120)
    chunks_400 = chunk_units(documents, 400, 60)
    keyword = KeywordStrategy("keyword (BM25, chunks of 1000)", chunks_1000)
    vector = VectorStrategy("vector, chunks of 1000 (current)", chunks_1000, embedder)
    return [
        RandomStrategy("random order (floor)", [d.doc_id for d in documents]),
        keyword,
        VectorStrategy("vector, whole documents", whole_document_units(documents), embedder),
        vector,
        VectorStrategy("vector, chunks of 400", chunks_400, embedder),
        HybridStrategy("hybrid RRF (BM25 + vector, chunks of 1000)", [keyword, vector]),
    ]


def format_report(evaluations: list[Evaluation], ks: tuple[int, ...] = DEFAULT_KS) -> str:
    columns = list(evaluations[0].summary(ks))
    width = max(len(e.name) for e in evaluations)
    lines = [f"{'strategy'.ljust(width)}  " + "  ".join(c.rjust(8) for c in columns)]
    lines.append("-" * len(lines[0]))
    for e in evaluations:
        summary = e.summary(ks)
        lines.append(f"{e.name.ljust(width)}  " + "  ".join(f"{summary[c]:8.3f}" for c in columns))

    categories = sorted({r.query.category for r in evaluations[0].results})
    lines.append("")
    lines.append("by query category (hit@3 / MRR):")
    header = "category".ljust(12) + "".join(e.name[:22].rjust(24) for e in evaluations)
    lines.append(header)
    for category in categories:
        row = category.ljust(12)
        for e in evaluations:
            s = e.by_category(ks)[category]
            row += f"{s['hit@3']:.2f} / {s['mrr']:.2f}".rjust(24)
        n = sum(1 for r in evaluations[0].results if r.query.category == category)
        lines.append(row + f"   (n={n})")
    return "\n".join(lines)


def vocabulary_overlap(query: EvalQuery, documents: list[Document]) -> float:
    """问题里的词，有多大比例也出现在正确答案文档里（0 = 完全不同的用词，1 = 全部出现过）。

    这是评估数据集的【难度诊断】。语义检索的价值在于"用词不同但意思相近"也能找到；
    如果标注的问题和正确答案的用词高度重叠，关键词检索就能轻松答对，
    评估就测不出向量检索的优势：不是向量检索不行，是题目太容易。
    标注数据和语料由同一个人写，很容易不自觉地复用文档里的词，所以要把这个数字摆出来。
    """
    wanted = set(query.relevant)
    document_words = {w for d in documents if d.doc_id in wanted for w in tokenize(f"{d.title} {d.body}")}
    query_words = set(tokenize(query.query))
    return len(query_words & document_words) / len(query_words) if query_words else 0.0


def format_overlap(queries: list[EvalQuery], documents: list[Document]) -> str:
    by_category: dict[str, list[float]] = defaultdict(list)
    for q in queries:
        by_category[q.category].append(vocabulary_overlap(q, documents))
    lines = ["share of query words that also appear in the relevant documents (lower = harder for keyword search):"]
    for category in sorted(by_category):
        values = by_category[category]
        lines.append(f"  {category.ljust(11)} average {sum(values) / len(values):.2f}  (lowest {min(values):.2f})")
    return "\n".join(lines)


def format_failures(evaluation: Evaluation, k: int = 3) -> str:
    """列出这个方案在前 k 名里没找到任何正确答案的问题：分析"为什么错"比看平均数更有用。"""
    lines = [f"{evaluation.name}: queries with no relevant document in the top {k}"]
    misses = [r for r in evaluation.results if r.rank is None or r.rank > k]
    if not misses:
        lines.append("  none")
    for r in misses:
        shown = ", ".join(r.ranking[:3]) or "(no results)"
        lines.append(f"  [{r.query.id}] {r.query.query[:70]!r}\n      wanted {sorted(r.relevant)}, got {shown}")
    return "\n".join(lines)
