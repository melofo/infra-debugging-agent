"""评估并比较几种检索方案的质量。完全在内存里运行，不需要数据库。

    .venv/bin/python scripts/eval_retrieval.py --embedder hash       # 离线，不花钱（只反映用词重叠）
    .venv/bin/python scripts/eval_retrieval.py                       # 用 .env 里配置的向量化器（Gemini：真实的语义质量）
    .venv/bin/python scripts/eval_retrieval.py --details             # 同时列出每个方案答错的问题

用 hash 向量化器得到的数字【不代表真实的检索质量】：它没有语义，只比较用词。
要回答"向量检索比关键词好多少"，必须用 Gemini 向量化器。
Gemini 模式会向量化约 150 段文字（分批发送，几次请求）。
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# 直接运行脚本时，Python 的模块搜索路径里是脚本所在的目录（scripts/）而不是项目根目录。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.llm.base import LLMError  # noqa: E402
from app.llm.factory import LLMNotConfigured  # noqa: E402
from app.rag.documents import load_documents  # noqa: E402
from app.rag.embedder import create_embedder  # noqa: E402
from app.rag.evaluation import (  # noqa: E402
    CachedEmbedder,
    build_strategies,
    evaluate,
    format_failures,
    format_overlap,
    format_report,
    load_queries,
)


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    if args.embedder:
        settings = settings.model_copy(update={"embedding_provider": args.embedder})
    try:
        embedder = CachedEmbedder(create_embedder(settings))
    except LLMNotConfigured as exc:
        print(f"Cannot create the embedder: {exc}")
        return 2

    documents = load_documents()
    queries = load_queries()
    print(f"embedder: {embedder.id} | documents: {len(documents)} | labeled queries: {len(queries)}\n")

    try:
        evaluations = [await evaluate(strategy, queries) for strategy in build_strategies(documents, embedder)]
    except LLMError as exc:
        print(f"Embedding failed: {type(exc).__name__}: {str(exc)[:300]}")
        return 4

    print(format_report(evaluations))
    print("\n" + format_overlap(queries, documents))
    print(f"\n({embedder.calls} texts were sent to the embedder)")
    if embedder.id.startswith("hash"):
        print("NOTE: the hash embedder compares words only. These numbers say nothing about semantic quality.")
    if args.details:
        for evaluation in evaluations:
            print("\n" + format_failures(evaluation))
    if args.json:
        Path(args.json).write_text(
            json.dumps({e.name: {"summary": e.summary(), "by_category": e.by_category()} for e in evaluations}, indent=2)
        )
        print(f"\nresults written to {args.json}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare retrieval strategies on the labeled queries.")
    parser.add_argument("--embedder", choices=["gemini", "hash"], help="override EMBEDDING_PROVIDER from .env")
    parser.add_argument("--details", action="store_true", help="list the queries each strategy gets wrong")
    parser.add_argument("--json", help="also write the numbers to this file")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
