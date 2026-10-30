"""POST /chat：第一个调用 LLM 的接口（后面会被真正的调查 Agent 取代/扩展）。"""

import logging
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.llm.base import LLMClient, LLMError
from app.llm.factory import LLMNotConfigured, create_llm_client

router = APIRouter()
# 每个模块用 __name__ 创建自己的 logger（Java 里的 LoggerFactory.getLogger(Foo.class)）。
logger = logging.getLogger(__name__)

# 系统提示词（system prompt）：告诉模型它的角色和行为规则。
SYSTEM_PROMPT = (
    "You are an infrastructure debugging assistant helping engineers investigate "
    "production incidents. Be concise and precise. Never state a root cause as fact "
    "without evidence; if you lack data, say so instead of guessing."
)


# 请求体和响应体都定义成 Pydantic 模型：
# - 请求不合法（空消息、太长）时，FastAPI 自动返回 422，不会进到我们的函数。
# - response_model 保证返回结构固定，并写入 /docs 的接口文档。
class ChatRequest(BaseModel):
    # 限制最大长度很重要：输入越长，花的 token（钱）越多，也能防止滥用。
    message: str = Field(min_length=1, max_length=2000)


class ChatResponse(BaseModel):
    reply: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


# 这是一个"依赖提供者"。
# lru_cache 让 GeminiClient 只创建一次、之后复用（单例），避免每个请求都重新建立客户端。
@lru_cache
def get_llm_client() -> LLMClient:
    try:
        return create_llm_client(get_settings())
    except LLMNotConfigured:
        # 503 = 服务暂时不可用。这里表示"服务端还没配置好"，而不是调用方的错。
        raise HTTPException(status_code=503, detail="LLM is not configured")


@router.post("/chat", response_model=ChatResponse)
# Depends(...) 是 FastAPI 的依赖注入，相当于 Spring 的 @Autowired：
# 请求进来时，FastAPI 先调用 get_llm_client() 拿到对象，再传给 llm 参数。
# 好处：测试时可以用 app.dependency_overrides 把它换成假的 LLM（见 tests/test_chat.py）。
async def chat(request: ChatRequest, llm: LLMClient = Depends(get_llm_client)) -> ChatResponse:
    try:
        result = await llm.generate(request.message, system=SYSTEM_PROMPT)
    except LLMError:
        # logger.exception 会连同堆栈一起记录。
        # 502 = Bad Gateway：我们自己没问题，是上游（LLM 服务）出了问题。
        # 注意：给客户端的 detail 是笼统的，不泄露内部细节；详细原因只写进日志。
        logger.exception("llm call failed")
        raise HTTPException(status_code=502, detail="LLM provider error")

    # extra 里的字段会成为 JSON 日志的独立字段，方便以后按 latency_ms、token 数做统计。
    # 注意：这里故意不记录用户消息内容，故障描述里可能含敏感信息，日志里不该留。
    logger.info(
        "llm call succeeded",
        extra={
            "latency_ms": result.latency_ms,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        },
    )
    return ChatResponse(
        reply=result.text,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=result.latency_ms,
    )
