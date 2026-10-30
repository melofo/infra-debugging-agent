"""创建 LLM 客户端。

之前这段逻辑写在 /chat 的接口文件里。现在 Worker 也需要同样的客户端，
所以抽到这里共用：两处都不需要知道怎么拼配置，只管调用 create_llm_client。
"""

from app.config import Settings
from app.llm.base import LLMClient
from app.llm.gemini import GeminiClient


class LLMNotConfigured(Exception):
    """没有配置 API key 或模型名。"""


def create_llm_client(settings: Settings) -> LLMClient:
    if not settings.gemini_api_key or not settings.gemini_model:
        raise LLMNotConfigured("GEMINI_API_KEY and GEMINI_MODEL must be set")
    return GeminiClient(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_attempts=settings.llm_max_attempts,
    )
