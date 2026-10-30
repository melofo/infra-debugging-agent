"""决定一次调查要不要使用知识库检索。"""

import logging

from app.rag.retrieval import KnowledgeBase

logger = logging.getLogger(__name__)


async def active_knowledge_base(knowledge: KnowledgeBase | None) -> KnowledgeBase | None:
    """返回可用的知识库；不可用就返回 None，调用方会改用关键词版的 runbook 检索兜底。

    "不可用"有几种情况：没有配置向量化器、pgvector 不可用、知识库还没入库、
    或者入库用的模型和当前配置的不一致。每次调查前检查一次（一条很轻的查询），
    这样你入库之后不需要重启服务，下一次调查就会用上知识库。
    """
    if knowledge is None:
        return None
    if await knowledge.has_documents():
        return knowledge
    logger.warning("knowledge base has no documents for the current embedding model; using keyword runbook search")
    return None
