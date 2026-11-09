"""LLMClient 的第二个实现：任何"OpenAI 兼容接口"的模型服务（Ollama、vLLM、OpenAI 本身等）。

【这就是适配器模式的第二次检验】agent 循环、工具白名单、证据核对、回滚、评估一行都不用改，
只新增这一个文件：把【我们自己的格式】（Message / ToolSpec / ToolCall / LLMResponse）
和【OpenAI 的 chat completions 格式】互相翻译。这个格式已经成了行业事实标准，开源推理服务基本都兼容它，
所以一个适配器就能接 Ollama（本地）、vLLM（Cloud Run GPU 上）和 OpenAI。

各家格式的几个差别，都由这里挡住：
  工具调用的参数    OpenAI 是【JSON 字符串】，Gemini 是对象 -> 这里 json.loads
  工具调用的 id     OpenAI 要求回传结果时带上 id，我们的 ToolCall 没有 id 字段
                    -> 这里按顺序生成 call_0、call_1…，回传时按同样的顺序对应（循环保证结果和调用一一对应、同序）
  工具结果          OpenAI 每个结果是一条单独的 role=tool 消息，Gemini 是一条消息里的多个 part
  思考内容          有些模型会返回 reasoning 字段，这里直接忽略，不混进答案里

用 httpx 直接调用（项目本来就依赖它），不引入 openai 这个 SDK：这个适配器只用到一个接口，几十行就够，少一个依赖。
"""

import asyncio
import json
import time

import httpx

from app.llm.base import LLMError, LLMResponse, Message, ToolCall, ToolSpec

RETRY_STATUS = {429, 503}  # 和 Gemini 一样：只重试"过一会儿可能就好"的错误


class OpenAICompatClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 120.0,  # 本地模型第一次加载、长上下文都会慢，比云端 API 宽松
        max_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
        temperature: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,  # 测试时换成假的传输层，不联网（类似 Java 里注入 MockWebServer）
    ):
        self._model = model
        self._temperature = temperature
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay_seconds
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}  # Ollama 本地不需要密钥
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_seconds, transport=transport
        )

    async def generate(self, prompt: str, system: str | None = None) -> LLMResponse:
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        return await self._complete({"messages": messages})

    async def generate_with_tools(
        self, messages: list[Message], tools: list[ToolSpec], system: str | None = None
    ) -> LLMResponse:
        body = {
            "messages": ([{"role": "system", "content": system}] if system else []) + _to_openai_messages(messages),
            "tools": [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ],
        }
        return await self._complete(body)

    async def _complete(self, body: dict) -> LLMResponse:
        body = {"model": self._model, **body}
        if self._temperature is not None:  # 0 也要传（0 是"假值"，不能写成 if self._temperature）
            body["temperature"] = self._temperature
        start = time.perf_counter()
        data = await self._post(body)
        return _to_llm_response(data, start)

    async def _post(self, body: dict) -> dict:
        last = "no attempt was made"
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._http.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:  # 连不上、超时：本地服务可能刚启动，值得重试
                last = f"{type(exc).__name__}"
            else:
                if response.status_code in RETRY_STATUS:
                    last = f"HTTP {response.status_code}"
                elif response.is_success:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise LLMError("model server returned invalid JSON") from exc
                else:
                    # 其他错误（400 参数错、401 密钥错、404 模型不存在）重试没用。只带状态码和一小段说明，不带请求头（里面有密钥）。
                    raise LLMError(f"model server error HTTP {response.status_code}: {response.text[:200]}")
            if attempt < self._max_attempts:
                await asyncio.sleep(self._retry_delay * 2 ** (attempt - 1))  # 指数退避
        raise LLMError(f"model server unavailable after {self._max_attempts} attempts ({last})")

    async def aclose(self) -> None:
        await self._http.aclose()


def _to_openai_messages(messages: list[Message]) -> list[dict]:
    """我们的 Message -> OpenAI 的 messages。"""
    out: list[dict] = []
    for message in messages:
        if message.role == "user":
            out.append({"role": "user", "content": message.text})
        elif message.role == "model":
            entry: dict = {"role": "assistant", "content": message.text}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {"id": f"call_{i}", "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.args, ensure_ascii=False)}}
                    for i, c in enumerate(message.tool_calls)
                ]
            out.append(entry)  # 注意：不使用 message.raw，那是别家模型的原始对象
        else:  # tool：每个结果一条消息，按顺序对应上一条 assistant 消息里的 call_i
            for i, result in enumerate(message.tool_results):
                payload = {"error": result.result} if result.is_error else result.result
                content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
                out.append({"role": "tool", "tool_call_id": f"call_{i}", "content": content})
    return out


def _to_llm_response(data: dict, start: float) -> LLMResponse:
    """OpenAI 的响应 -> 我们的 LLMResponse。"""
    choices = data.get("choices") or []
    message = (choices[0].get("message") if choices else None) or {}
    text = message.get("content") or ""
    tool_calls = tuple(_to_tool_call(c) for c in message.get("tool_calls") or [])
    if not text and not tool_calls:
        raise LLMError("model returned an empty response")
    usage = data.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    return LLMResponse(
        text=text,
        input_tokens=usage.get("prompt_tokens") or 0,
        output_tokens=usage.get("completion_tokens") or 0,
        latency_ms=int((time.perf_counter() - start) * 1000),
        tool_calls=tool_calls,
        cached_input_tokens=details.get("cached_tokens") or 0,
    )


def _to_tool_call(raw: dict) -> ToolCall:
    function = raw.get("function") or {}
    arguments = function.get("arguments")
    if isinstance(arguments, dict):  # 个别服务直接给对象，也接受
        args = arguments
    else:
        try:
            args = json.loads(arguments) if arguments else {}
        except ValueError:
            args = {}  # 小模型偶尔会生成坏掉的 JSON：给空参数，让工具的参数校验报错并回传给模型，它常常能自己改对
    return ToolCall(name=function.get("name") or "", args=args if isinstance(args, dict) else {})
