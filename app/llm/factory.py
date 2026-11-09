"""创建 LLM 客户端。

之前这段逻辑写在 /chat 的接口文件里。现在 Worker 也需要同样的客户端，
所以抽到这里共用：两处都不需要知道怎么拼配置，只管调用 create_llm_client。
"""

from urllib.parse import urlsplit

from google.auth.exceptions import DefaultCredentialsError

from app.config import Settings
from app.llm.base import LLMClient
from app.llm.gemini import GeminiClient
from app.llm.openai_compat import GoogleIdTokenAuth, OpenAICompatClient


class LLMNotConfigured(Exception):
    """没有配置 API key 或模型名。"""


VERTEX_CREDENTIALS_HELP = (
    "Vertex AI needs Google credentials: locally run `gcloud auth application-default login`; on Google Cloud attach a "
    "service account with the Vertex AI User role to the service"
)


def create_llm_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "openai-compatible":
        if not settings.llm_base_url or not settings.llm_model:
            raise LLMNotConfigured("LLM_BASE_URL and LLM_MODEL must be set when LLM_PROVIDER=openai-compatible")
        auth = None
        if settings.llm_auth == "google-id-token":
            # 令牌的"受众"是服务的根地址（去掉 /v1 这样的路径）：Cloud Run 用它核对令牌是发给哪个服务的。
            parts = urlsplit(settings.llm_base_url)
            auth = GoogleIdTokenAuth(f"{parts.scheme}://{parts.netloc}")
        return OpenAICompatClient(
            auth=auth,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key.get_secret_value(),
            timeout_seconds=settings.llm_compat_timeout_seconds,
            max_attempts=settings.llm_max_attempts,
            temperature=settings.llm_temperature,
        )
    if settings.llm_provider == "vertex":
        if not settings.gcp_project or not settings.gemini_model:
            raise LLMNotConfigured("GCP_PROJECT and GEMINI_MODEL must be set when LLM_PROVIDER=vertex")
        try:
            return GeminiClient(
                model=settings.gemini_model,
                timeout_seconds=settings.llm_timeout_seconds,
                max_attempts=settings.llm_max_attempts,
                vertex_project=settings.gcp_project,
                vertex_location=settings.gcp_location,
                temperature=settings.llm_temperature,
                thinking_budget=settings.llm_thinking_budget,
            )
        except DefaultCredentialsError as exc:
            # 没有找到凭证：给一条能照着做的提示，而不是 SDK 里一大段看不懂的异常。
            raise LLMNotConfigured(VERTEX_CREDENTIALS_HELP) from exc
    if not settings.gemini_api_key or not settings.gemini_model:
        raise LLMNotConfigured("GEMINI_API_KEY and GEMINI_MODEL must be set")
    return GeminiClient(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_attempts=settings.llm_max_attempts,
        temperature=settings.llm_temperature,
        thinking_budget=settings.llm_thinking_budget,
    )
