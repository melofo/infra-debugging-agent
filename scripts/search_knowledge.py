"""在知识库里做语义检索，看看会找到什么。

    .venv/bin/python scripts/search_knowledge.py "connection pool exhausted"
    .venv/bin/python scripts/search_knowledge.py "app cannot get a link to the datastore under heavy traffic" -k 3
    .venv/bin/python scripts/search_knowledge.py "memory keeps growing" --type runbook --service search-service

每次查询会调用一次向量化接口（把问题变成向量），所以会花一点点额度。
需要先运行 scripts/ingest_knowledge.py 入库，并且查询用的向量化模型要和入库时的一致。
"""

import argparse
import asyncio
import sys
from pathlib import Path

# 直接运行脚本时，Python 的模块搜索路径里是脚本所在的目录（scripts/）而不是项目根目录。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.db import create_pool  # noqa: E402
from app.llm.base import LLMError  # noqa: E402
from app.llm.factory import LLMNotConfigured  # noqa: E402
from app.rag.embedder import create_embedder  # noqa: E402
from app.rag.retrieval import KnowledgeBase, KnowledgeIndexMismatch  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    if args.embedder:
        settings = settings.model_copy(update={"embedding_provider": args.embedder})
    try:
        embedder = create_embedder(settings)
    except LLMNotConfigured as exc:
        print(f"Cannot create the embedder: {exc}")
        return 2

    pool = create_pool(settings.database_url, settings.db_pool_max_size, settings.db_pool_timeout_seconds)
    try:
        await pool.open(wait=True, timeout=10)
        hits = await KnowledgeBase(pool, embedder).search(
            args.query,
            k=args.k,
            doc_types=[args.type] if args.type else None,
            services=[args.service] if args.service else None,
            min_score=args.min_score,
            max_per_doc=args.max_per_doc,
        )
    except ValueError as exc:
        print(f"Invalid search: {exc}")
        return 2
    except KnowledgeIndexMismatch as exc:
        print(f"Index mismatch: {exc}")
        return 3
    except LLMError as exc:
        print(f"Embedding failed: {type(exc).__name__}: {str(exc)[:300]}")
        return 4
    except Exception as exc:
        print(f"Search failed ({type(exc).__name__}). Is the database running and the knowledge base ingested?")
        return 5
    finally:
        await pool.close()

    print(f"query: {args.query!r}  | embedder: {embedder.id}")
    if not hits:
        print("no results (is the knowledge base ingested? try scripts/ingest_knowledge.py)")
        return 0
    for rank, hit in enumerate(hits, start=1):
        snippet = " ".join(hit.content.split())[:110]
        print(f"{rank}. {hit.score:.3f}  {hit.doc_id:<9} {hit.title[:48]}")
        print(f"       [{hit.heading}] {snippet}...")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Semantic search over the knowledge base.")
    parser.add_argument("query")
    parser.add_argument("-k", type=int, default=5, help="number of results (1-20)")
    parser.add_argument("--type", choices=["runbook", "incident"], help="only this document type")
    parser.add_argument("--service", help="only documents for this service (plus generic ones)")
    parser.add_argument("--min-score", type=float, help="drop results below this cosine similarity")
    parser.add_argument("--max-per-doc", type=int, help="at most this many chunks per document")
    parser.add_argument("--embedder", choices=["gemini", "hash"], help="override EMBEDDING_PROVIDER from .env")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
