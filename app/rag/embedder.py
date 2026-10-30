"""向量化（embedding）：把一段文字变成一个固定长度的数字向量。

【核心思想】意思相近的文字，向量在空间里的位置也相近。检索时把问题也变成向量，
找"离它最近"的文档块，就不要求问题和文档用词一致了：
    "Connection is not available, request timed out" 和 "connection pool exhausted"
字面几乎没有重叠，但语义相近，向量就会靠近。关键词匹配做不到这一点。

【两种实现】
  GeminiEmbedder  调用 Gemini 的向量化接口，真正的语义向量。需要 API key，花额度。
  HashEmbedder    离线的"特征哈希"向量，不调用任何服务，结果完全可重复。
                  它只反映【用词重叠】，不是真正的语义，所以只用于：测试、没有 key 时的本地开发。
                  拿它测出来的检索质量不代表真实质量（真实质量要用 Gemini 评估，见 Phase 5.6）。

【维度固定为 768】pgvector 的 HNSW 索引最多支持 2000 维，而 Gemini 的默认输出是 3072 维。
Gemini 的向量化模型支持"降维输出"，我们让它直接输出 768 维：索引能建、存储和检索更省，
质量损失通常很小。数据库表的列类型是 vector(768)，这里必须一致。
"""

import hashlib
import math
import re
from collections import Counter
from typing import Protocol

from google import genai
from google.genai import types

from app.config import Settings
from app.llm.base import LLMError
from app.llm.factory import LLMNotConfigured
from app.llm.gemini import build_http_options

EMBEDDING_DIMENSIONS = 768


class EmbeddingError(LLMError):
    """向量化失败。继承 LLMError：Worker 对它的处理和模型调用失败一样（按退避重试）。"""


class Embedder(Protocol):
    @property
    def id(self) -> str:
        """向量化器的标识（含模型名和维度）。入库时记下它：换了模型，旧向量就不能和新向量比较，必须重新入库。"""
        ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


def normalize(vector: list[float]) -> list[float]:
    """缩放成长度为 1 的向量（L2 归一化）。

    Gemini 在降维输出时，向量长度不再是 1。比较向量方向（余弦相似度）时，统一成单位长度，
    结果才可靠，也让"内积"和"余弦"等价。
    """
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        raise ValueError("cannot normalize a zero vector")
    return [x / norm for x in vector]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(normalize(a), normalize(b)))


# ---------------------------------------------------------------------------
# 离线向量化器
# ---------------------------------------------------------------------------
_STOPWORDS = frozenset("a an and are as at be by for from in is it of on or that the this to was were with".split())


class HashEmbedder:
    """特征哈希（feature hashing / hashing trick）：把每个词（和相邻词对）哈希到向量的某一位上累加。

    没有任何模型，所以速度极快、结果确定。相似的文字（用词重叠多）向量相似。
    """

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS):
        self._dimensions = dimensions

    @property
    def id(self) -> str:
        return f"hash:{self._dimensions}"

    def _embed(self, text: str) -> list[float]:
        words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS]
        if not words:
            raise ValueError("cannot embed text without any words")
        # 单词 + 相邻词对：词对保留一点点词序信息（"pool size" 比单独的 "pool" 和 "size" 更有区分度）。
        features = Counter(words + [f"{a}_{b}" for a, b in zip(words, words[1:])])
        vector = [0.0] * self._dimensions
        for feature, count in features.items():
            # 【不能用 Python 内置的 hash()】它对字符串的结果每次进程启动都不一样（防哈希碰撞攻击），
            # 同一段文字今天和明天向量化得到不同结果，存进数据库的向量就和查询向量对不上了。
            # 用 blake2b 这种确定性的哈希。
            digest = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
            sign = 1.0 if (digest >> 63) & 1 else -1.0  # 随机的正负号，让不同词的哈希冲突互相抵消而不是叠加
            vector[digest % self._dimensions] += sign * (1.0 + math.log(count))  # 对数词频：出现 10 次不等于重要 10 倍
        return normalize(vector)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


# ---------------------------------------------------------------------------
# Gemini 向量化器
# ---------------------------------------------------------------------------
class GeminiEmbedder:
    def __init__(
        self,
        api_key: str = "",
        model: str = "gemini-embedding-001",
        dimensions: int = EMBEDDING_DIMENSIONS,
        timeout_seconds: float = 30.0,
        max_attempts: int = 4,
        batch_size: int = 50,
        client=None,  # 测试时注入假客户端
    ):
        self._model = model
        self._dimensions = dimensions
        self._batch_size = batch_size
        self._client = client or genai.Client(api_key=api_key, http_options=build_http_options(timeout_seconds, max_attempts))

    @property
    def id(self) -> str:
        return f"gemini:{self._model}:{self._dimensions}"

    async def _embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        try:
            response = await self._client.aio.models.embed_content(
                model=self._model,
                contents=texts,
                # task_type 告诉模型这段文字的用途：文档和问题的向量化方式略有不同，
                # 分别用 RETRIEVAL_DOCUMENT 和 RETRIEVAL_QUERY 能提升检索效果。
                config=types.EmbedContentConfig(task_type=task_type, output_dimensionality=self._dimensions),
            )
        except Exception as exc:  # SDK 可能抛多种异常，统一包装
            raise EmbeddingError(f"embedding request failed: {exc}") from exc

        embeddings = response.embeddings or []
        if len(embeddings) != len(texts):
            raise EmbeddingError(f"expected {len(texts)} embeddings, got {len(embeddings)}")
        vectors = []
        for item in embeddings:
            values = list(item.values or [])
            if len(values) != self._dimensions:
                # 维度不对（比如模型不支持降维）会让数据库拒绝写入，在这里提前给出清楚的错误。
                raise EmbeddingError(f"expected {self._dimensions} dimensions, got {len(values)}")
            vectors.append(normalize(values))
        return vectors

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        # 分批发送：一次请求能带的文本数量有上限。
        for start in range(0, len(texts), self._batch_size):
            vectors += await self._embed(texts[start : start + self._batch_size], "RETRIEVAL_DOCUMENT")
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text], "RETRIEVAL_QUERY"))[0]


def create_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "hash":
        return HashEmbedder()
    if not settings.gemini_api_key:
        raise LLMNotConfigured("GEMINI_API_KEY must be set to use Gemini embeddings (or set EMBEDDING_PROVIDER=hash)")
    return GeminiEmbedder(
        api_key=settings.gemini_api_key,
        model=settings.embedding_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_attempts=settings.llm_max_attempts,
    )
