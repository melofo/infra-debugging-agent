"""把对话历史（Message）转成 JSON 再还原：会话要存进数据库，就得能无损地"存进去再取出来"。

【难点在 Message.raw】
raw 是模型厂商返回的原始对象（Gemini 的 Content），里面带着 thought signature 这类内部标记，
下一轮请求必须原样带回，否则会被拒绝（见 Message.raw 的说明）。所以不能丢，也不能只存文字。
我们让厂商自己的类型来序列化（pydantic 的 model_dump / model_validate），并在 JSON 里记下它是什么类型，
读回来时才知道该还原成哪个类。字节数据（signature）会被编码成 base64 文本，来回都是无损的。

【不认识的 raw 类型：拒绝，而不是悄悄丢掉】
悄悄丢掉 raw，存进去时一切正常，等到几天后会话恢复、发给模型时才被拒绝，那时已经无从排查。
宁可保存的时候就报错，让问题在第一次出现的地方暴露。
"""

from typing import Any

from google.genai import types as genai_types

from app.llm.base import Message, ToolCall, ToolResult

GENAI_CONTENT = "genai.Content"


def _encode_raw(raw: Any) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, genai_types.Content):
        return {"type": GENAI_CONTENT, "data": raw.model_dump(mode="json", exclude_none=True)}
    raise TypeError(f"cannot persist raw message data of type {type(raw).__name__}")


def _decode_raw(encoded: dict | None) -> Any:
    if encoded is None:
        return None
    if encoded.get("type") == GENAI_CONTENT:
        return genai_types.Content.model_validate(encoded["data"])
    raise ValueError(f"unknown raw message type {encoded.get('type')!r}")


def message_to_json(message: Message) -> dict:
    return {
        "role": message.role,
        "text": message.text,
        "tool_calls": [{"name": c.name, "args": c.args} for c in message.tool_calls],
        "tool_results": [{"name": r.name, "result": r.result, "is_error": r.is_error} for r in message.tool_results],
        "raw": _encode_raw(message.raw),
    }


def message_from_json(data: dict) -> Message:
    return Message(
        role=data["role"],
        text=data.get("text", ""),
        tool_calls=tuple(ToolCall(c["name"], c["args"]) for c in data.get("tool_calls", [])),
        tool_results=tuple(
            ToolResult(r["name"], r["result"], r.get("is_error", False)) for r in data.get("tool_results", [])
        ),
        raw=_decode_raw(data.get("raw")),
    )
