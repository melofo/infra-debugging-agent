"""OpenAI 兼容适配器的测试（Ollama、vLLM、OpenAI 都说这种接口）。不联网：用 httpx 的假传输层 MockTransport。

最重要的一个测试是最后那个：让【真正的 agent 循环】通过这个适配器跑完一次调查。
这证明"加第二家模型只需要新增一个适配器，循环、工具、证据核对一行都不用改"不是一句口号。
"""

import json

import httpx
import pytest

from app.agent.loop import InvestigationAgent
from app.agent.tools import build_investigation_tools
from app.config import Settings
from app.llm.base import LLMError, Message, ToolCall, ToolResult, ToolSpec
from app.llm.factory import LLMNotConfigured, create_llm_client
from app.llm.openai_compat import OpenAICompatClient, _to_openai_messages
from app.scenario import load_scenario
from tests.fakes import SUBMIT_TOOL_NAME, VALID_REPORT_ARGS


def answer(text="hello", tool_calls=None, usage=None):
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}], "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5}}


def call(name, args, id="x"):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Server:
    """假的模型服务：记录收到的每个请求，按预设依次回答（可以是响应字典，也可以是状态码）。"""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return httpx.Response(reply, text="boom") if isinstance(reply, int) else httpx.Response(200, json=reply)

    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


def client(server, **kw):
    kw.setdefault("retry_delay_seconds", 0.0)
    return OpenAICompatClient("http://model.local/v1", "m", transport=httpx.MockTransport(server), **kw)


TOOL = ToolSpec(name="search_logs", description="d", parameters={"type": "object", "properties": {"service": {"type": "string"}}})


# ---------------------------------------------------------------- 请求：我们的格式 -> OpenAI 的格式
async def test_a_plain_question_sends_the_system_prompt_and_the_model_name():
    server = Server(answer())
    await client(server).generate("hi", system="be brief")
    body = server.bodies()[0]
    assert body["model"] == "m" and body["messages"] == [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]
    assert str(server.requests[0].url) == "http://model.local/v1/chat/completions"


async def test_tools_are_declared_in_the_openai_function_format():
    server = Server(answer())
    await client(server).generate_with_tools([Message(role="user", text="go")], [TOOL])
    assert server.bodies()[0]["tools"] == [{"type": "function", "function": {"name": "search_logs", "description": "d", "parameters": TOOL.parameters}}]


def test_history_with_tool_calls_is_translated_with_matching_call_ids_and_json_string_arguments():
    history = [
        Message(role="user", text="investigate"),
        Message(role="model", text="", tool_calls=(ToolCall("search_logs", {"service": "a"}), ToolCall("get_ticket", {"ticket_id": "B-1"})), raw="vendor object, must be ignored"),
        Message(role="tool", tool_results=(ToolResult("search_logs", {"lines": 2}), ToolResult("get_ticket", "no such ticket", is_error=True))),
    ]
    out = _to_openai_messages(history)
    assistant = out[1]
    assert [c["id"] for c in assistant["tool_calls"]] == ["call_0", "call_1"]
    assert assistant["tool_calls"][0]["function"] == {"name": "search_logs", "arguments": '{"service": "a"}'}  # 参数是 JSON 字符串
    assert out[2] == {"role": "tool", "tool_call_id": "call_0", "content": '{"lines": 2}'}
    assert out[3] == {"role": "tool", "tool_call_id": "call_1", "content": '{"error": "no such ticket"}'}  # 同序对应，错误也带上
    assert "vendor object" not in json.dumps(out)  # 别家的原始对象不会被带进请求


@pytest.mark.parametrize("given, sent", [(None, "absent"), (0.0, 0.0), (0.7, 0.7)])
async def test_temperature_is_only_sent_when_set_and_zero_counts(given, sent):
    server = Server(answer())
    await client(server, temperature=given).generate("hi")
    body = server.bodies()[0]
    assert (body.get("temperature", "absent")) == sent


async def test_the_api_key_header_is_only_sent_when_a_key_is_configured():
    with_key, without = Server(answer()), Server(answer())
    await client(with_key, api_key="secret").generate("hi")
    await client(without).generate("hi")
    assert with_key.requests[0].headers["authorization"] == "Bearer secret" and "authorization" not in without.requests[0].headers


# ---------------------------------------------------------------- 响应：OpenAI 的格式 -> 我们的格式
async def test_text_tool_calls_and_usage_are_translated():
    server = Server(answer("thinking done", [call("search_logs", {"service": "a"})], {"prompt_tokens": 100, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 80}}))
    response = await client(server).generate_with_tools([Message(role="user", text="go")], [TOOL])
    assert response.text == "thinking done" and response.tool_calls == (ToolCall("search_logs", {"service": "a"}),)
    assert (response.input_tokens, response.output_tokens, response.cached_input_tokens) == (100, 7, 80)


async def test_the_reasoning_field_is_not_mixed_into_the_answer():
    reply = answer("final")
    reply["choices"][0]["message"]["reasoning"] = "secret chain of thought"
    response = await client(Server(reply)).generate("hi")
    assert response.text == "final"


@pytest.mark.parametrize("arguments, expected", [('{"service": "a"}', {"service": "a"}), ({"service": "a"}, {"service": "a"}), ("{broken json", {}), ("", {}), ("[1, 2]", {})])
async def test_tool_arguments_that_are_malformed_become_empty_so_validation_can_report_them(arguments, expected):
    reply = answer("", [{"id": "1", "type": "function", "function": {"name": "search_logs", "arguments": arguments}}])
    response = await client(Server(reply)).generate_with_tools([Message(role="user", text="go")], [TOOL])
    assert response.tool_calls == (ToolCall("search_logs", expected),)


async def test_an_empty_answer_is_an_error_not_a_silent_success():
    with pytest.raises(LLMError, match="empty response"):
        await client(Server(answer(""))).generate("hi")


# ---------------------------------------------------------------- 错误和重试
async def test_a_busy_server_is_retried_and_then_succeeds():
    server = Server(429, 503, answer("ok"))
    assert (await client(server, max_attempts=3).generate("hi")).text == "ok" and len(server.requests) == 3


async def test_retries_are_bounded():
    server = Server(503)
    with pytest.raises(LLMError, match="unavailable after 3 attempts"):
        await client(server, max_attempts=3).generate("hi")
    assert len(server.requests) == 3


@pytest.mark.parametrize("status", [400, 401, 404])
async def test_client_errors_are_not_retried(status):
    server = Server(status)
    with pytest.raises(LLMError, match=f"HTTP {status}"):
        await client(server, max_attempts=3).generate("hi")
    assert len(server.requests) == 1


async def test_connection_failures_are_retried_then_reported():
    def refuse(request):
        raise httpx.ConnectError("refused")

    flaky = OpenAICompatClient("http://model.local/v1", "m", max_attempts=2, retry_delay_seconds=0.0, transport=httpx.MockTransport(refuse))
    with pytest.raises(LLMError, match="unavailable after 2 attempts"):
        await flaky.generate("hi")


async def test_the_error_message_never_contains_the_api_key():
    server = Server(401)
    with pytest.raises(LLMError) as info:
        await client(server, api_key="super-secret-key").generate("hi")
    assert "super-secret-key" not in str(info.value)


# ---------------------------------------------------------------- 配置和工厂
def settings(**kw):
    return Settings(_env_file=None, llm_provider="openai-compatible", **kw)


def test_the_factory_builds_the_client_and_requires_a_url_and_a_model():
    assert isinstance(create_llm_client(settings(llm_base_url="http://localhost:11434/v1", llm_model="gemma4:26b")), OpenAICompatClient)
    for missing in (dict(llm_base_url="http://x/v1"), dict(llm_model="m"), {}):
        with pytest.raises(LLMNotConfigured, match="LLM_BASE_URL and LLM_MODEL"):
            create_llm_client(settings(**missing))


def test_the_factory_passes_every_setting_through():
    llm = create_llm_client(settings(llm_base_url="http://x/v1/", llm_model="gemma4:26b", llm_api_key="k", llm_temperature=0, llm_compat_timeout_seconds=77, llm_max_attempts=5))
    assert llm._model == "gemma4:26b" and llm._temperature == 0 and llm._max_attempts == 5
    assert llm._http.headers["authorization"] == "Bearer k"  # 来自配置里的密钥，不是测试自己加的
    assert str(llm._http.base_url) == "http://x/v1/" and llm._http.timeout.read == 77


def test_the_gemini_defaults_are_untouched():
    assert Settings.model_fields["llm_provider"].default == "gemini-api"


# ---------------------------------------------------------------- 端到端：真正的 agent 循环 + 这个适配器
async def test_the_real_agent_loop_completes_an_investigation_through_the_adapter():
    """查工单 -> 提交报告 -> 文字收尾。循环里没有任何一行知道背后是 OpenAI 格式的服务。"""
    server = Server(
        answer("", [call("get_ticket", {"ticket_id": "BUG-1042"})]),
        answer("", [call(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)]),
        answer("Investigation complete."),
    )
    agent = InvestigationAgent(client(server), build_investigation_tools(load_scenario("bug-1042")))
    result = await agent.investigate("BUG-1042")
    assert result.report is not None and result.report.hypotheses[0].confidence == 0.9
    # 第二次请求的历史里，工具结果是按 OpenAI 的规矩回传的：assistant 的 tool_calls + 对应 id 的 tool 消息
    second = server.bodies()[1]["messages"]
    assert second[-2]["tool_calls"][0]["id"] == "call_0" and second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "call_0"
    assert "BUG-1042" in second[-1]["content"]  # 工具真的执行了，返回的是真实的工单数据


async def test_a_bad_report_is_sent_back_through_the_adapter_and_the_model_can_fix_it():
    """自我纠错也走得通：编造的证据被拒，原因作为工具结果回传，第二次提交通过。"""
    fabricated = {**VALID_REPORT_ARGS, "hypotheses": [{**VALID_REPORT_ARGS["hypotheses"][0], "evidence": [{"source": "log", "summary": "s", "reference": "a line no tool ever returned"}]}]}
    server = Server(
        answer("", [call("get_ticket", {"ticket_id": "BUG-1042"})]),
        answer("", [call(SUBMIT_TOOL_NAME, fabricated)]),
        answer("", [call(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)]),
        answer("Done."),
    )
    agent = InvestigationAgent(client(server), build_investigation_tools(load_scenario("bug-1042")))
    result = await agent.investigate("BUG-1042")
    assert result.report is not None
    rejection = server.bodies()[2]["messages"][-1]
    assert rejection["role"] == "tool" and "error" in rejection["content"]
