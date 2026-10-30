"""Gemini 适配层的测试：不联网，只测"我们的格式 <-> Gemini 的格式"的转换。

适配器是最容易出 bug 的地方（各家厂商格式不同），所以值得单独测试。
"""

from google.genai import types

from app.agent.common import SUBMIT_TOOL_NAME
from app.llm.base import Message, ToolCall, ToolResult
from app.llm.gemini import _to_gemini_contents, _to_llm_response


def make_response(parts: list[types.Part]) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=7, candidates_token_count=3),
    )


def test_response_with_function_call_is_parsed_into_tool_calls():
    response = make_response([types.Part(function_call=types.FunctionCall(name="get_ticket", args={"ticket_id": "BUG-1042"}))])
    result = _to_llm_response(response, start=0.0)

    assert result.text == ""
    assert result.tool_calls == (ToolCall("get_ticket", {"ticket_id": "BUG-1042"}),)
    assert (result.input_tokens, result.output_tokens) == (7, 3)
    # 原始输出被保存，下一轮会原样带回。
    assert result.raw is response.candidates[0].content


def test_thought_parts_are_excluded_from_text():
    response = make_response([types.Part(text="internal reasoning", thought=True), types.Part(text="answer")])
    assert _to_llm_response(response, start=0.0).text == "answer"


def test_empty_response_is_an_error():
    import pytest

    from app.llm.base import LLMError

    with pytest.raises(LLMError):
        _to_llm_response(types.GenerateContentResponse(candidates=[]), start=0.0)


def test_history_is_converted_with_function_results_as_user_turn():
    history = [
        Message(role="user", text="Investigate ticket BUG-1042."),
        Message(role="model", tool_calls=(ToolCall("search_runbooks", {"query": "pool"}),)),
        Message(role="tool", tool_results=(ToolResult("search_runbooks", [{"id": "RB-101"}]),)),
    ]
    contents = _to_gemini_contents(history)

    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1].parts[0].function_call.name == "search_runbooks"
    # 列表结果被包成 JSON 对象，因为 Gemini 要求 function_response 是对象。
    assert contents[2].parts[0].function_response.response == {"result": [{"id": "RB-101"}]}


def test_raw_model_output_is_reused_verbatim():
    raw = types.Content(role="model", parts=[types.Part(text="x")])
    contents = _to_gemini_contents([Message(role="model", text="ignored", raw=raw)])
    assert contents[0] is raw


def test_python_functions_become_tool_declarations_for_sdk_afc():
    # 版本 B：SDK 会从函数签名+docstring 生成说明书。这里提前验证我们写的签名它能读懂（不联网）。
    from app.agent.sdk_afc import SdkAfcAgent  # noqa: F401  确认模块可导入

    def search_logs(service: str, query: str = "", level: str | None = None, limit: int = 20) -> dict:
        """Search logs.

        Args:
            service: Service name.
        """
        return {}

    declaration = types.FunctionDeclaration.from_callable_with_api_option(
        callable=search_logs, api_option="GEMINI_API"
    )
    assert declaration.name == "search_logs"
    assert "service" in declaration.parameters.properties


def test_async_python_functions_also_become_tool_declarations_for_sdk_afc():
    # 工具包装函数现在是 async 的（它们要访问数据库）。SDK 要能照样从签名和 docstring 生成说明书，
    # 并且知道该 await 它们。这个测试确认两件事。
    import inspect

    async def search_knowledge(query: str, doc_type: str | None = None, limit: int = 5) -> dict:
        """Search runbooks and past incidents by meaning.

        Args:
            query: Symptom description in natural language.
            doc_type: runbook or incident.
            limit: Max results.
        """
        return {}

    declaration = types.FunctionDeclaration.from_callable_with_api_option(
        callable=search_knowledge, api_option="GEMINI_API"
    )
    assert declaration.name == "search_knowledge"
    assert {"query", "doc_type", "limit"} <= set(declaration.parameters.properties)
    assert declaration.parameters.required == ["query"]  # 只有没有默认值的参数是必填
    assert inspect.iscoroutinefunction(search_knowledge)  # SDK 靠这个判断要不要 await


def test_submit_tool_name_is_stable():
    # 提示词、循环、SDK 版本都按这个名字识别"终止工具"，改名要同步改动，所以锁住它。
    assert SUBMIT_TOOL_NAME == "submit_rca_report"
