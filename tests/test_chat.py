"""/chat 的测试。关键技巧：用"假的 LLM"替换真的，测试不联网、不花钱、结果确定。

这就是依赖注入的价值，和 Java 里用 Mockito mock 掉一个 Service 是同一个思路。
"""

from fastapi.testclient import TestClient

from app.api.chat import get_llm_client
from app.config import Settings
from app.llm.base import LLMError, LLMResponse
from app.main import app


# 假的 LLM：只要有同名的 generate 方法就满足 LLMClient 协议（鸭子类型），不需要 implements。
class FakeLLM:
    async def generate(self, prompt: str, system: str | None = None) -> LLMResponse:
        return LLMResponse(text=f"echo: {prompt}", input_tokens=5, output_tokens=3, latency_ms=12)


class FailingLLM:
    async def generate(self, prompt: str, system: str | None = None) -> LLMResponse:
        raise LLMError("boom")


def make_client(llm) -> TestClient:
    # dependency_overrides：告诉 FastAPI "有人要 get_llm_client 时，改给他这个假对象"。
    app.dependency_overrides[get_llm_client] = lambda: llm
    return TestClient(app)


# pytest 在每个测试函数结束后自动调用 teardown_function（相当于 JUnit 的 @AfterEach），
# 清理覆盖设置，避免一个测试影响另一个。
def teardown_function():
    app.dependency_overrides.clear()


def test_chat_returns_reply_and_usage():
    response = make_client(FakeLLM()).post("/chat", json={"message": "hi"})
    assert response.status_code == 200
    assert response.json() == {
        "reply": "echo: hi",
        "input_tokens": 5,
        "output_tokens": 3,
        "latency_ms": 12,
    }


def test_chat_rejects_empty_message():
    # 空消息在 Pydantic 校验阶段就被拒绝（422），根本不会调用 LLM。
    response = make_client(FakeLLM()).post("/chat", json={"message": ""})
    assert response.status_code == 422


def test_chat_maps_llm_failure_to_502():
    response = make_client(FailingLLM()).post("/chat", json={"message": "hi"})
    assert response.status_code == 502


def test_chat_without_config_returns_503(monkeypatch):
    # 教训：测试不能依赖开发者本机的 .env！之前没有 .env 时它恰好能过，
    # 配了真实 key 后它会真的调用 Gemini（花钱、受网络影响）。
    # monkeypatch 是 pytest 内置的"临时替换"工具（类似 Mockito 的 when().thenReturn()），
    # 测试结束后自动还原。这里让 chat 模块拿到一份"没有 key"的配置：
    # _env_file=None 表示不读 .env 文件，显式传入的参数优先级最高，也盖过环境变量。
    no_key_settings = Settings(_env_file=None, gemini_api_key="", gemini_model="")
    monkeypatch.setattr("app.api.chat.get_settings", lambda: no_key_settings)
    # get_llm_client 被 lru_cache 缓存过，先清掉，确保这次真的走一遍"没配置"的逻辑。
    get_llm_client.cache_clear()
    response = TestClient(app).post("/chat", json={"message": "hi"})
    assert response.status_code == 503
