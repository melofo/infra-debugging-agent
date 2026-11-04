"""LLMClient 的 Gemini 实现，使用 Google 官方 SDK google-genai。

这个类提供【两条】使用工具的路径，用来对比学习：
  A. generate_with_tools(...)        我们自己的 agent loop 使用。每次只发【一次】请求，
                                      并显式关闭 SDK 的自动循环，控制权留在我们手里。
  B. generate_with_python_tools(...) 使用 SDK 的 AFC（Automatic Function Calling）。
                                      SDK 在一次调用内部替我们跑完整个循环。
"""

import time
from collections.abc import AsyncIterator, Callable

from google import genai
from google.genai import types

from app.llm.base import LLMError, LLMResponse, Message, ToolCall, ToolSpec


def build_http_options(timeout_seconds: float, max_attempts: int) -> types.HttpOptions:
    """超时和重试策略，聊天客户端和向量化客户端共用。"""
    return types.HttpOptions(
        timeout=int(timeout_seconds * 1000),
        # 重试策略：只重试"过一会儿可能就好"的临时性错误。
        #   429 = 限流（请求太频繁，免费额度是每分钟固定次数）
        #   503 = 服务过载（"high demand"）
        # 其他错误（400 参数错、401 key 错）重试也没用，不重试。
        # 指数退避（exponential backoff）：等待时间逐次翻倍（约 8s、16s、32s），
        # 加随机抖动（jitter）避免很多客户端同时重试把服务再次打垮。
        # 注意：重试会让一次调用变得更久，这类等待在生产里应该放进后台任务。
        retry_options=types.HttpRetryOptions(
            attempts=max_attempts,  # 含第一次请求，所以 4 表示最多重试 3 次
            initial_delay=8.0,
            max_delay=40.0,
            exp_base=2.0,
            http_status_codes=[429, 503],
        ),
    )


def build_genai_client(
    *,
    api_key: str = "",
    vertex_project: str = "",
    vertex_location: str = "",
    timeout_seconds: float = 30.0,
    max_attempts: int = 4,
) -> genai.Client:
    """创建 Google GenAI SDK 的客户端：对话客户端和向量化器共用，这样两者永远走同一种通道。

    两种通道，调用的是同一批 Gemini 模型，区别在于【怎么证明你是谁】：
      API key（Google AI Studio）  一串密钥，放在环境变量里；免费额度小，GCP 的 $300 试用金不能用在这里。
      Vertex AI（Google Cloud）    用服务账号的身份（Application Default Credentials）：没有密钥可以泄露，
                                   权限由 IAM 控制，费用记在 GCP 账单上，企业客户的标准做法。
    给了 vertex_project 就走 Vertex AI；否则走 API key。
    """
    http_options = build_http_options(timeout_seconds, max_attempts)
    if vertex_project:
        # 注意：不能同时传 api_key，SDK 会认为你搞混了。
        return genai.Client(vertexai=True, project=vertex_project, location=vertex_location or "us-central1", http_options=http_options)
    return genai.Client(api_key=api_key, http_options=http_options)


class GeminiClient:
    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        timeout_seconds: float = 30.0,
        max_attempts: int = 4,
        vertex_project: str = "",
        vertex_location: str = "",
        temperature: float | None = None,
        thinking_budget: int | None = None,
    ):
        self._model = model  # 下划线开头是约定：表示"内部使用"，类似 Java 的 private
        # temperature：模型每生成一个 token 时"怎么从候选里选"的旋钮。0 = 几乎总选概率最高的（稳定、可复现），
        # 越高越随机。None = 不传，用模型自己的默认值（和加这个参数之前的行为完全一样）。
        self._temperature = temperature
        # 扩展思考（extended thinking）：让模型在回答前先"想一会儿"。thinking_budget = 允许它用来思考的 token 数上限
        # （0 = 关闭，-1 = 让模型自己决定，None = 不传，用模型默认）。思考也要计费、也会变慢，所以是个要权衡的开关。
        # 思考的内容不会混进我们的回答：_to_llm_response 已经跳过了 part.thought。
        self._thinking = types.ThinkingConfig(thinking_budget=thinking_budget) if thinking_budget is not None else None
        # 超时必须设置：LLM 偶尔会卡很久，没有超时的话，一个慢请求会一直占着资源。
        # SDK 的超时单位是毫秒，所以乘以 1000。
        self._client = build_genai_client(
            api_key=api_key, vertex_project=vertex_project, vertex_location=vertex_location,
            timeout_seconds=timeout_seconds, max_attempts=max_attempts,
        )

    async def generate(self, prompt: str, system: str | None = None) -> LLMResponse:
        # perf_counter 是测量耗时用的单调时钟，比 time.time() 更适合算时间差。
        start = time.perf_counter()
        try:
            # system_instruction（系统提示词）：给模型设定角色和规则，和用户消息分开传。
            # self._client.aio 是 SDK 的异步版本，对应上面接口里的 async。
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(system_instruction=system, temperature=self._temperature, thinking_config=self._thinking),
            )
        except Exception as exc:  # SDK 可能抛多种异常，这里统一转成我们自己的 LLMError
            # `from exc` 保留原始异常链，排查问题时能看到根本原因（类似 Java 的 cause）。
            raise LLMError(f"Gemini request failed: {exc}") from exc
        return _to_llm_response(response, start)

    async def generate_stream(self, prompt: str, system: str | None = None) -> AsyncIterator[str]:
        """逐段返回模型的文字（边生成边给，不用等全部写完）。

        用在"人在等着看"的场景（聊天界面）：第一个字出来得快，体感更快，总耗时不变。
        我们的 agent 循环不用它：agent 需要拿到完整的一轮输出（文字 + 工具调用）才能决定下一步。
        目前只有 Gemini 实现，没有放进 LLMClient 接口；要用在界面上时再决定是否提升为接口。
        """
        config = types.GenerateContentConfig(system_instruction=system, temperature=self._temperature, thinking_config=self._thinking)
        try:
            stream = await self._client.aio.models.generate_content_stream(model=self._model, contents=prompt, config=config)
            async for chunk in stream:
                parts = (chunk.candidates[0].content.parts if chunk.candidates and chunk.candidates[0].content else None) or []
                text = "".join(p.text for p in parts if p.text and not p.thought)  # 跳过思考内容
                if text:
                    yield text
        except Exception as exc:
            raise LLMError(f"Gemini stream failed: {exc}") from exc

    # ---------------------------------------------------------------
    # 路径 A：手写 agent loop 用的单次请求
    # ---------------------------------------------------------------
    async def generate_with_tools(
        self, messages: list[Message], tools: list[ToolSpec], system: str | None = None
    ) -> LLMResponse:
        start = time.perf_counter()
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self._temperature,
            thinking_config=self._thinking,
            # 只传"工具声明"（名字+描述+参数结构），没有可执行函数。
            # 这样 SDK 的 function_map 为空，即使 AFC 开着也会在第一次请求后 break（见 models.py）。
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=spec.name,
                            description=spec.description,
                            parameters_json_schema=spec.parameters,
                        )
                        for spec in tools
                    ]
                )
            ],
            # 第二道保险：显式关闭 AFC，保证"一次调用 = 一次请求"，循环由我们自己控制。
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=_to_gemini_contents(messages),
                config=config,
            )
        except Exception as exc:
            raise LLMError(f"Gemini request failed: {exc}") from exc
        return _to_llm_response(response, start)

    # ---------------------------------------------------------------
    # 路径 B：使用 SDK 的 AFC（框架帮你跑循环）
    # ---------------------------------------------------------------
    async def generate_with_python_tools(
        self,
        prompt: str,
        functions: list[Callable],
        system: str | None = None,
        max_remote_calls: int = 8,
    ) -> LLMResponse:
        """把普通 Python 函数直接交给 SDK。

        SDK 会：1) 读函数的类型标注和 docstring 自动生成工具声明；
               2) 模型要调用时，自动执行这些函数；3) 自动把结果回传，循环直到模型给出最终回答。
        代价：循环藏在这一次调用里面，我们看不到中间每一步，也很难在执行前插入权限检查或审批。
        另外返回的 usage（token 用量）只是【最后一次】请求的，不是全部请求的总和。
        """
        start = time.perf_counter()
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self._temperature,
            thinking_config=self._thinking,
            tools=functions,  # 直接传可调用的函数，SDK 的 function_map 非空 → 触发自动循环
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=max_remote_calls
            ),
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model, contents=prompt, config=config
            )
        except Exception as exc:
            raise LLMError(f"Gemini request failed: {exc}") from exc
        return _to_llm_response(response, start, require_content=False)


def _to_gemini_contents(messages: list[Message]) -> list[types.Content]:
    """把我们自己的 Message 转成 Gemini 的格式。

    这是"适配器"（adapter）：Agent 只认识我们的 Message，厂商差异都挡在这个文件里。
    Gemini 里只有 user / model 两种 role，工具结果也算 user 发的，放在 function_response 里。
    """
    contents: list[types.Content] = []
    for message in messages:
        if message.role == "model" and message.raw is not None:
            # 模型上一轮的原始输出原样带回（见 Message.raw 的说明）。
            contents.append(message.raw)
        elif message.role == "model":
            parts = [types.Part(text=message.text)] if message.text else []
            parts += [
                types.Part(function_call=types.FunctionCall(name=c.name, args=c.args))
                for c in message.tool_calls
            ]
            contents.append(types.Content(role="model", parts=parts))
        elif message.role == "tool":
            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_function_response(
                            name=r.name,
                            # Gemini 要求 response 是一个 JSON 对象，所以列表、字符串要包一层。
                            response=(
                                {"error": r.result}
                                if r.is_error
                                else r.result
                                if isinstance(r.result, dict)
                                else {"result": r.result}
                            ),
                        )
                        for r in message.tool_results
                    ],
                )
            )
        else:
            contents.append(types.Content(role="user", parts=[types.Part(text=message.text)]))
    return contents


def _to_llm_response(
    response: types.GenerateContentResponse, start: float, require_content: bool = True
) -> LLMResponse:
    """把 Gemini 的响应转成我们自己的 LLMResponse。"""
    candidate = response.candidates[0] if response.candidates else None
    parts = (candidate.content.parts if candidate and candidate.content else None) or []

    # 不用 response.text：里面夹着函数调用时它会打警告。我们自己从 parts 里拼文字，
    # 并跳过模型的"思考内容"（part.thought）。
    text = "".join(p.text for p in parts if p.text and not p.thought)
    tool_calls = tuple(
        ToolCall(name=p.function_call.name, args=dict(p.function_call.args or {}))
        for p in parts
        if p.function_call
    )

    # 模型可能因为安全过滤等原因什么都没返回，要当成错误，不能默默返回空结果。
    if require_content and not text and not tool_calls:
        raise LLMError("Gemini returned an empty response")

    # usage_metadata 里有 token 用量；可能缺失，所以用 `or 0` 兜底。
    usage = response.usage_metadata
    return LLMResponse(
        text=text,
        input_tokens=(usage.prompt_token_count or 0) if usage else 0,
        output_tokens=(usage.candidates_token_count or 0) if usage else 0,
        cached_input_tokens=(usage.cached_content_token_count or 0) if usage else 0,
        latency_ms=int((time.perf_counter() - start) * 1000),
        tool_calls=tool_calls,
        raw=candidate.content if candidate else None,
    )
