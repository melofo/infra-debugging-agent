"""把 data/knowledge/ 里的文档入库（切块、向量化、写进 pgvector）。

    .venv/bin/python scripts/ingest_knowledge.py                    # 用 .env 里配置的向量化器
    .venv/bin/python scripts/ingest_knowledge.py --dry-run          # 只看会做什么，不调用接口、不写库
    .venv/bin/python scripts/ingest_knowledge.py --embedder hash    # 用离线向量化器（不花钱，只适合测试）

可以重复运行：没变的文档会被跳过，不会重新调用向量化接口。
"""

import argparse
import asyncio
import sys
from pathlib import Path

# 直接运行脚本时，Python 的模块搜索路径里是脚本所在的目录（scripts/）而不是项目根目录。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.db import create_pool, init_knowledge_schema, init_schema  # noqa: E402
from app.llm.base import LLMError  # noqa: E402
from app.llm.factory import LLMNotConfigured  # noqa: E402
from app.rag.documents import KNOWLEDGE_DIR, load_documents  # noqa: E402
from app.rag.embedder import create_embedder  # noqa: E402
from app.rag.ingest import ingest  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    if args.embedder:
        settings = settings.model_copy(update={"embedding_provider": args.embedder})

    try:
        documents = load_documents(Path(args.dir))
    except ValueError as exc:
        print(f"Invalid knowledge document: {exc}")
        return 2
    try:
        embedder = create_embedder(settings)
    except LLMNotConfigured as exc:
        print(f"Cannot create the embedder: {exc}")
        return 2

    pool = create_pool(settings.database_url, settings.db_pool_max_size, settings.db_pool_timeout_seconds)
    try:
        await pool.open(wait=True, timeout=10)
        await init_schema(pool)
        if not await init_knowledge_schema(pool):
            print("The pgvector extension is not available in this database.")
            return 3
        print(f"embedder: {embedder.id} | documents found: {len(documents)}")
        report = await ingest(pool, embedder, documents, delete_missing=not args.no_delete, dry_run=args.dry_run)
    except LLMError as exc:
        print(f"Embedding failed; the database was not changed. {type(exc).__name__}: {str(exc)[:300]}")
        return 4
    except Exception as exc:  # 连不上数据库等
        print(f"Ingestion failed ({type(exc).__name__}). Is the database running? (docker compose up -d db)")
        return 5
    finally:
        await pool.close()

    for label, ids in (("added", report.added), ("updated", report.updated), ("deleted", report.deleted)):
        if ids:
            print(f"  {label}: {', '.join(ids)}")
    print(report.summary())
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest the knowledge base into pgvector.")
    parser.add_argument("--dir", default=str(KNOWLEDGE_DIR), help="directory with the markdown documents")
    parser.add_argument("--embedder", choices=["gemini", "hash"], help="override EMBEDDING_PROVIDER from .env")
    parser.add_argument("--dry-run", action="store_true", help="show what would change without calling the API or writing")
    parser.add_argument("--no-delete", action="store_true", help="keep database documents that are no longer in the directory")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
