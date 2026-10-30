"""检索评估代码的测试。

评估代码本身要先被验证：指标公式写错了，后面所有"A 方案比 B 方案好"的结论就都不可信。
所以这里用手算得出答案的小例子，逐个核对公式。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from psycopg.conninfo import make_conninfo

from app.config import Settings
from app.rag.documents import load_documents
from app.rag.embedder import HashEmbedder
from app.rag.evaluation import (
    Bm25Index,
    CachedEmbedder,
    EvalQuery,
    HybridStrategy,
    KeywordStrategy,
    QueryResult,
    RandomStrategy,
    Unit,
    VectorStrategy,
    aggregate,
    build_strategies,
    chunk_units,
    evaluate,
    first_relevant_rank,
    format_failures,
    format_report,
    hit_at,
    load_queries,
    rank_documents,
    recall_at,
    reciprocal_rank,
    tokenize,
    vocabulary_overlap,
)
from app.rag.ingest import ingest
from app.rag.retrieval import KnowledgeBase

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_retrieval.py"


def query(qid="q", relevant=("A",), category="symptom", text="some query text") -> EvalQuery:
    return EvalQuery(id=qid, category=category, query=text, relevant=list(relevant))


# ------------------------------------------------------------ 指标（手算验证）
def test_first_relevant_rank_is_one_based():
    assert first_relevant_rank(["x", "A", "y"], {"A"}) == 2
    assert first_relevant_rank(["A"], {"A"}) == 1
    assert first_relevant_rank(["x", "y"], {"A"}) is None


def test_hit_at_k():
    ranking = ["x", "y", "A", "z"]
    assert hit_at(ranking, {"A"}, 2) == 0.0  # A 在第 3 名，前 2 名里没有
    assert hit_at(ranking, {"A"}, 3) == 1.0
    assert hit_at(ranking, {"A", "z"}, 1) == 0.0  # 只要有一个正确答案就算命中


def test_recall_at_k_counts_the_share_of_relevant_documents_found():
    ranking = ["A", "x", "B", "y", "C"]
    assert recall_at(ranking, {"A", "B", "C"}, 1) == pytest.approx(1 / 3)
    assert recall_at(ranking, {"A", "B", "C"}, 3) == pytest.approx(2 / 3)
    assert recall_at(ranking, {"A", "B", "C"}, 5) == pytest.approx(1.0)


def test_reciprocal_rank():
    assert reciprocal_rank(["A", "x"], {"A"}) == 1.0
    assert reciprocal_rank(["x", "A"], {"A"}) == 0.5
    assert reciprocal_rank(["x", "y", "A"], {"A"}) == pytest.approx(1 / 3)
    assert reciprocal_rank(["x", "y"], {"A"}) == 0.0  # 根本没找到
    assert reciprocal_rank(["x", "B", "A"], {"A", "B"}) == 0.5  # 只看第一个正确答案


def test_aggregate_averages_over_queries():
    results = [
        QueryResult(query("q1", ["A"]), ["A", "x", "y"]),  # hit@1=1, rr=1
        QueryResult(query("q2", ["B", "C"]), ["x", "B", "C"]),  # hit@1=0, hit@3=1, recall@3=1, rr=0.5
        QueryResult(query("q3", ["D"]), ["x", "y", "z"]),  # 全错
    ]
    summary = aggregate(results, ks=(1, 3))
    assert summary["hit@1"] == pytest.approx(1 / 3)
    assert summary["hit@3"] == pytest.approx(2 / 3)
    assert summary["recall@3"] == pytest.approx((1 + 1 + 0) / 3)
    assert summary["mrr"] == pytest.approx((1 + 0.5 + 0) / 3)


# ------------------------------------------------------------ 排序辅助
def test_ranking_keeps_only_the_best_unit_of_each_document():
    units = [Unit("A", "a1"), Unit("B", "b1"), Unit("A", "a2"), Unit("C", "c1")]
    # 得分：a1=0.1, b1=0.9, a2=0.5, c1=0.3 -> B(0.9), A(0.5，取它的最佳块), C(0.3)
    assert rank_documents(units, [0.1, 0.9, 0.5, 0.3]) == ["B", "A", "C"]


def test_equal_scores_keep_the_original_order_so_results_are_repeatable():
    units = [Unit("A", ""), Unit("B", ""), Unit("C", "")]
    assert rank_documents(units, [1.0, 1.0, 1.0]) == ["A", "B", "C"]


# ------------------------------------------------------------ BM25
def test_tokenizer_lowercases_and_drops_stopwords():
    assert tokenize("The HikariPool-1 is exhausted, and the 500s!") == ["hikaripool", "1", "exhausted", "500s"]


def test_a_rare_term_is_worth_more_than_a_common_one():
    # 要让"稀有度"成为唯一的变量：前两段文字等长、各自只命中一个查询词、各出现一次。
    # 唯一的区别是：rare 只在一段里出现，common 在五段里都出现。
    # （我的第一版测试里，第一段同时命中两个查询词，不管稀有度怎么算它都是最高分，变异检查才发现这个测试没有隔离这一点。）
    texts = ["rare filler", "common filler", "common alpha", "common beta", "common gamma"]
    scores = Bm25Index(texts).scores("rare common")
    assert scores[0] > scores[1]


def test_a_term_that_never_appears_scores_zero_everywhere():
    assert Bm25Index(["alpha beta", "gamma"]).scores("zebra") == [0.0, 0.0]


def test_repeating_a_term_helps_but_with_diminishing_returns():
    index = Bm25Index(["pool", "pool pool", "pool pool pool pool pool pool pool pool", "other words here"])
    one, two, many, _ = index.scores("pool")
    assert one < two < many
    assert (two - one) > (many - two) / 6  # 增长越来越慢：出现 8 次不等于重要 8 倍


def test_longer_text_is_penalised_for_the_same_term_count():
    index = Bm25Index(["pool", "pool " + "filler " * 40])
    short, long = index.scores("pool")
    assert short > long


async def test_keyword_strategy_returns_nothing_instead_of_inventing_an_order():
    strategy = KeywordStrategy("kw", [Unit("A", "alpha beta"), Unit("B", "gamma")])
    assert await strategy.rank("zebra") == []
    assert await strategy.rank("gamma") == ["B", "A"]  # B 含有这个词排第一；A 没有，得分为 0 排在后面


# ------------------------------------------------------------ 倒数排名融合
class Fixed:
    def __init__(self, name, ranking):
        self.name, self._ranking = name, ranking

    async def rank(self, query):
        return self._ranking


async def test_rrf_rewards_documents_that_several_strategies_agree_on():
    hybrid = HybridStrategy("h", [Fixed("a", ["x", "y"]), Fixed("b", ["z", "y"])])
    # y 在两个排名里都出现（第 2 名 + 第 2 名），x、z 各只在一个排名里排第 1。
    # y: 1/62 + 1/62 = 0.0323 > x: 1/61 = 0.0164
    assert (await hybrid.rank("q"))[0] == "y"


async def test_rrf_uses_ranks_not_raw_scores_and_includes_documents_seen_by_only_one_strategy():
    hybrid = HybridStrategy("h", [Fixed("a", ["x"]), Fixed("b", ["w"])])
    assert set(await hybrid.rank("q")) == {"x", "w"}


async def test_rrf_ties_are_broken_by_first_appearance():
    hybrid = HybridStrategy("h", [Fixed("a", ["x", "y"]), Fixed("b", ["y", "x"])])
    assert await hybrid.rank("q") == ["x", "y"]  # 得分完全相同，按第一次出现的顺序


async def test_rrf_formula_matches_a_hand_calculation():
    hybrid = HybridStrategy("h", [Fixed("a", ["x", "y", "z"])], c=10)
    # 只有一个排名：得分 1/(10+1) > 1/(10+2) > 1/(10+3)，顺序不变。
    assert await hybrid.rank("q") == ["x", "y", "z"]


# ------------------------------------------------------------ 随机基线
async def test_the_random_baseline_is_repeatable_per_query_but_varies_between_queries():
    strategy = RandomStrategy("r", ["A", "B", "C", "D", "E", "F", "G", "H"])
    first = await strategy.rank("same question")
    assert first == await strategy.rank("same question")
    assert sorted(first) == ["A", "B", "C", "D", "E", "F", "G", "H"]  # 每篇文档恰好出现一次
    others = [await strategy.rank(f"other {i}") for i in range(5)]  # 异步调用不能写在 any(...) 的生成器里
    assert any(other != first for other in others)


# ------------------------------------------------------------ 向量化缓存
async def test_cached_embedder_sends_each_text_only_once():
    cached = CachedEmbedder(HashEmbedder())
    await cached.embed_documents(["alpha beta", "gamma delta", "alpha beta"])  # 重复的只算一次
    await cached.embed_documents(["alpha beta", "epsilon zeta"])  # 只有新的那一个会被发送
    await cached.embed_query("alpha beta")
    await cached.embed_query("alpha beta")
    assert cached.calls == 3 + 1  # 3 个不同的文档文字 + 1 个问题；问题和文档分开缓存（用途不同）


# ------------------------------------------------------------ 评估数据集的质量
def test_every_labeled_document_exists_and_ids_are_unique():
    corpus = {d.doc_id for d in load_documents()}
    queries = load_queries()
    assert len({q.id for q in queries}) == len(queries)
    for q in queries:
        assert set(q.relevant) <= corpus, q.id  # 标注里写了不存在的文档，评估结果就是错的


def test_every_document_is_the_right_answer_for_at_least_one_query():
    labeled = {doc for q in load_queries() for doc in q.relevant}
    assert labeled == {d.doc_id for d in load_documents()}  # 没有一篇文档从来没被考到


def test_the_disjoint_queries_really_avoid_the_vocabulary_of_their_answers():
    # "disjoint" 是专门检验语义检索的难题：问题和正确答案几乎没有相同用词。
    # 这里用代码强制保证，而不是凭作者的感觉。以后谁改了问题、不小心又用回文档里的词，这个测试会报错。
    documents = load_documents()
    disjoint = [q for q in load_queries() if q.category == "disjoint"]
    assert len(disjoint) >= 6
    for q in disjoint:
        assert vocabulary_overlap(q, documents) <= 0.3, (q.id, vocabulary_overlap(q, documents))
    assert sum(vocabulary_overlap(q, documents) for q in disjoint) / len(disjoint) <= 0.15


def test_vocabulary_overlap_computation():
    documents = load_documents()
    q = EvalQuery(id="x", category="symptom", query="connection pool exhaustion", relevant=["RB-101"])
    assert vocabulary_overlap(q, documents) == 1.0  # 三个词都出现在 RB-101 里
    # 没有词干还原：exhausted 和 exhaustion 算两个不同的词（真实的搜索引擎默认会做词干还原，所以这个基线偏弱）。
    q = EvalQuery(id="x2", category="symptom", query="connection pool exhausted", relevant=["RB-101"])
    assert vocabulary_overlap(q, documents) == pytest.approx(2 / 3)
    q = EvalQuery(id="y", category="disjoint", query="zebra giraffe", relevant=["RB-101"])
    assert vocabulary_overlap(q, documents) == 0.0


# ------------------------------------------------------------ 评估流程
class Oracle:
    name = "oracle"

    def __init__(self, queries):
        self._by_text = {q.query: q for q in queries}

    async def rank(self, query):
        q = self._by_text[query]
        return list(q.relevant) + ["zzz"]


class Worst:
    name = "worst"

    async def rank(self, query):
        return ["zzz1", "zzz2", "zzz3"]


async def test_a_perfect_strategy_scores_one_and_a_useless_one_scores_zero():
    queries = load_queries()
    perfect = (await evaluate(Oracle(queries), queries)).summary()
    useless = (await evaluate(Worst(), queries)).summary()

    assert perfect["hit@1"] == perfect["mrr"] == perfect["recall@5"] == 1.0
    assert all(value == 0.0 for value in useless.values())


async def test_all_default_strategies_run_and_produce_metrics_in_range():
    embedder = CachedEmbedder(HashEmbedder())
    queries = load_queries()
    strategies = build_strategies(load_documents(), embedder)

    assert [s.name.split(",")[0].split(" (")[0] for s in strategies][:3] == ["random order", "keyword", "vector"]
    for strategy in strategies:
        evaluation = await evaluate(strategy, queries)
        assert len(evaluation.results) == len(queries)
        assert all(0.0 <= v <= 1.0 for v in evaluation.summary().values()), strategy.name

    report = format_report([await evaluate(s, queries) for s in strategies])
    assert "disjoint" in report and "mrr" in report


async def test_failures_list_the_queries_a_strategy_gets_wrong():
    queries = load_queries()[:3]
    text = format_failures(await evaluate(Worst(), queries))
    assert all(f"[{q.id}]" in text for q in queries) and "wanted" in text


async def test_real_strategies_beat_the_random_floor_on_the_whole_set():
    # 基本的健全性检查：关键词和向量（哪怕只是离线的哈希向量）总体上都应当明显好过随机排序。
    embedder = CachedEmbedder(HashEmbedder())
    queries = load_queries()
    strategies = build_strategies(load_documents(), embedder)
    mrr = {s.name: (await evaluate(s, queries)).summary()["mrr"] for s in strategies}

    floor = mrr["random order (floor)"]
    assert all(value > floor + 0.2 for name, value in mrr.items() if name != "random order (floor)")


# ------------------------------------------------------------ 评估路径和线上路径一致
async def test_the_in_memory_ranking_matches_what_the_database_returns(knowledge_pool):
    # 评估在内存里做，线上检索走 SQL。如果两者排序不一致，评估结果就不能代表线上表现。
    # 对每个问题，用同样的向量化器和同样的切块方式，比较前 3 篇文档。
    documents = load_documents()
    embedder = HashEmbedder()
    await ingest(knowledge_pool, embedder, documents)
    knowledge = KnowledgeBase(knowledge_pool, embedder)
    in_memory = VectorStrategy("mem", chunk_units(documents, 1000, 120), CachedEmbedder(embedder))

    mismatches = []
    for q in load_queries():
        from_db: list[str] = []
        for hit in await knowledge.search(q.query, k=20):
            if hit.doc_id not in from_db:
                from_db.append(hit.doc_id)
        if from_db[:3] != (await in_memory.rank(q.query))[:3]:
            mismatches.append(q.id)
    assert mismatches == []


# ------------------------------------------------------------ 命令行脚本（像用户那样运行）
def run_script(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "GEMINI_API_KEY")}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120
    )


def test_the_script_prints_the_comparison_table_and_needs_no_database(tmp_path):
    result = run_script(tmp_path, "--embedder", "hash", "--json", str(tmp_path / "out.json"), "--details")

    assert result.returncode == 0, result.stdout + result.stderr
    for text in ("random order (floor)", "keyword (BM25", "hybrid RRF", "hit@3", "mrr", "disjoint", "share of query words"):
        assert text in result.stdout, text
    assert "say nothing about semantic quality" in result.stdout  # 对哈希向量化器的诚实提醒
    data = json.loads((tmp_path / "out.json").read_text())
    assert len(data) == 6 and all("mrr" in entry["summary"] for entry in data.values())


def test_the_script_without_an_api_key_explains_how_to_proceed(tmp_path):
    result = run_script(tmp_path, "--embedder", "gemini")
    assert result.returncode == 2 and "EMBEDDING_PROVIDER=hash" in result.stdout
