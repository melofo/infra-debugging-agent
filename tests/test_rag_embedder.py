"""向量化器的测试（不调用真实接口）。"""

import math
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.llm.base import LLMError
from app.llm.factory import LLMNotConfigured
from app.rag.embedder import (
    EMBEDDING_DIMENSIONS,
    EmbeddingError,
    GeminiEmbedder,
    HashEmbedder,
    cosine_similarity,
    create_embedder,
    normalize,
)


# ------------------------------------------------------------ 工具函数
def test_normalize_produces_unit_length():
    vector = normalize([3.0, 4.0])
    assert vector == pytest.approx([0.6, 0.8])
    assert math.sqrt(sum(x * x for x in vector)) == pytest.approx(1.0)


def test_normalizing_a_zero_vector_is_an_error_not_nan():
    with pytest.raises(ValueError):
        normalize([0.0, 0.0])


def test_cosine_similarity_basics():
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)
    assert cosine_similarity([1, 1], [2, 2]) == pytest.approx(1.0)  # 只比较方向，长度不影响


# ------------------------------------------------------------ 离线向量化器
async def test_hash_embedder_returns_unit_vectors_of_the_right_size():
    [vector] = await HashEmbedder().embed_documents(["Connection pool exhausted under load"])
    assert len(vector) == EMBEDDING_DIMENSIONS
    assert math.sqrt(sum(x * x for x in vector)) == pytest.approx(1.0)


async def test_hash_embedder_is_deterministic_across_calls_and_processes():
    text = "HikariPool-1 connection is not available"
    a = await HashEmbedder().embed_query(text)
    b = await HashEmbedder().embed_query(text)
    assert a == b

    # 跨进程也必须一致：这是不能用 Python 内置 hash() 的原因（它对字符串的结果每个进程都不同）。
    # 两个细节让这个测试真正有效（我第一版写得太弱，变异检查才发现）：
    #   1. 指纹必须覆盖【整个向量】。向量是稀疏的，只看前几个分量的话几乎永远是 0.0，什么变化都看不出来。
    #   2. 显式开启哈希随机化。如果环境里设置了固定的 PYTHONHASHSEED，内置 hash() 就恰好稳定，测试也就失效了。
    code = (
        "import asyncio; from app.rag.embedder import HashEmbedder;"
        "v = asyncio.run(HashEmbedder().embed_query('HikariPool-1 connection is not available'));"
        "print([(i, round(x, 6)) for i, x in enumerate(v) if x])"
    )
    env = {**os.environ, "PYTHONHASHSEED": "random"}
    outputs = {
        subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env).stdout
        for _ in range(3)
    }
    assert len(outputs) == 1  # 三个不同的进程，得到完全相同的向量
    assert outputs.pop().strip() == str([(i, round(x, 6)) for i, x in enumerate(a) if x])


async def test_similar_texts_are_closer_than_unrelated_ones():
    embedder = HashEmbedder()
    query = await embedder.embed_query("database connection pool exhausted timeout")
    related = await embedder.embed_query("The connection pool is exhausted and requests time out waiting for the database")
    unrelated = await embedder.embed_query("TLS certificate expired causing handshake failures at the gateway")

    assert cosine_similarity(query, related) > cosine_similarity(query, unrelated) + 0.1


async def test_identical_texts_have_similarity_one_and_word_order_matters_a_little():
    embedder = HashEmbedder()
    a = await embedder.embed_query("pool size reduced")
    assert cosine_similarity(a, await embedder.embed_query("pool size reduced")) == pytest.approx(1.0)
    assert cosine_similarity(a, await embedder.embed_query("reduced size pool")) < 0.999  # 词对保留了一点词序信息


@pytest.mark.parametrize("text", ["", "   ", "the a of"])
async def test_text_without_content_words_is_rejected(text):
    with pytest.raises(ValueError):
        await HashEmbedder().embed_query(text)


def test_embedder_ids_identify_the_model_and_dimensions():
    assert HashEmbedder().id == "hash:768"
    assert GeminiEmbedder(client=object(), model="m", dimensions=768).id == "gemini:m:768"


# ------------------------------------------------------------ Gemini 向量化器（用假客户端）
class FakeEmbeddingClient:
    """假的 SDK 客户端：记录收到的请求，返回预设的向量。"""

    def __init__(self, dimensions=768, drop_last=False, error=None):
        self.requests = []
        self.dimensions = dimensions
        self.drop_last = drop_last
        self.error = error
        self.aio = SimpleNamespace(models=SimpleNamespace(embed_content=self._embed_content))

    async def _embed_content(self, *, model, contents, config):
        self.requests.append({"model": model, "n": len(contents), "task": config.task_type, "dims": config.output_dimensionality})
        if self.error:
            raise self.error
        items = [SimpleNamespace(values=[2.0] + [0.0] * (self.dimensions - 1)) for _ in contents]
        return SimpleNamespace(embeddings=items[:-1] if self.drop_last else items)


async def test_gemini_embedder_batches_documents_and_normalizes_results():
    client = FakeEmbeddingClient()
    embedder = GeminiEmbedder(client=client, model="m", batch_size=2)

    vectors = await embedder.embed_documents(["a", "b", "c", "d", "e"])

    assert [r["n"] for r in client.requests] == [2, 2, 1]  # 5 个文本分成 3 批
    assert all(r["task"] == "RETRIEVAL_DOCUMENT" and r["dims"] == 768 and r["model"] == "m" for r in client.requests)
    assert len(vectors) == 5
    assert vectors[0][0] == pytest.approx(1.0)  # 模型返回的 [2, 0, ...] 被归一化成单位长度


async def test_gemini_embedder_uses_the_query_task_type_for_queries():
    client = FakeEmbeddingClient()
    await GeminiEmbedder(client=client).embed_query("why is checkout failing")
    assert client.requests[0]["task"] == "RETRIEVAL_QUERY"


async def test_wrong_dimensions_are_rejected_with_a_clear_error():
    with pytest.raises(EmbeddingError, match="expected 768 dimensions, got 3072"):
        await GeminiEmbedder(client=FakeEmbeddingClient(dimensions=3072)).embed_query("x")


async def test_a_missing_embedding_is_rejected():
    with pytest.raises(EmbeddingError, match="expected 2 embeddings, got 1"):
        await GeminiEmbedder(client=FakeEmbeddingClient(drop_last=True)).embed_documents(["a", "b"])


async def test_sdk_errors_are_wrapped_and_treated_like_llm_errors():
    embedder = GeminiEmbedder(client=FakeEmbeddingClient(error=RuntimeError("429 quota")))
    with pytest.raises(EmbeddingError) as error:
        await embedder.embed_query("x")
    assert isinstance(error.value, LLMError)  # Worker 会像对待模型调用失败一样按退避重试
    assert "429 quota" in str(error.value)


# ------------------------------------------------------------ 工厂
def test_factory_chooses_the_provider_from_settings():
    assert isinstance(create_embedder(Settings(_env_file=None, embedding_provider="hash")), HashEmbedder)
    gemini = create_embedder(Settings(_env_file=None, embedding_provider="gemini", gemini_api_key="k" * 20))
    assert isinstance(gemini, GeminiEmbedder)


def test_gemini_embeddings_without_an_api_key_are_a_configuration_error():
    with pytest.raises(LLMNotConfigured):
        create_embedder(Settings(_env_file=None, embedding_provider="gemini", gemini_api_key=""))
