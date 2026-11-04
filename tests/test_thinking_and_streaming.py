"""扩展思考（thinking budget）和逐段流式输出（generate_stream）的测试。不联网：把 SDK 客户端换成"记录员"。

思考预算：不配置时不往请求里放（行为不变）；配置成 0（关闭）时要【真的传过去】（0 是"假值"，别被 `if` 吃掉）。
流式：按顺序产出文字片段；思考内容不外泄；出错时统一成 LLMError，不泄露 SDK 的异常类型。
"""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.llm import gemini
from app.llm.base import LLMError
from app.llm.factory import create_llm_client


class FakeModels:
    def __init__(self, sink, chunks=None, error=None):
        self.sink, self.chunks, self.error = sink, chunks or [], error

    async def generate_content(self, *, model, contents, config):
        self.sink.append(config)
        raise RuntimeError("stop")

    async def generate_content_stream(self, *, model, contents, config):
        self.sink.append(config)
        if self.error:
            raise self.error

        async def gen():
            for chunk in self.chunks:
                yield chunk

        return gen()


def chunk(*parts):
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text=t, thought=th) for t, th in parts]))])


@pytest.fixture
def sink(monkeypatch):
    configs: list = []
    holder = {"chunks": [], "error": None}

    class FakeClient:
        def __init__(self, **kwargs):
            self.aio = SimpleNamespace(models=FakeModels(configs, holder["chunks"], holder["error"]))

    monkeypatch.setattr(gemini.genai, "Client", FakeClient)
    return SimpleNamespace(configs=configs, holder=holder)


def call_all_three_paths(client):
    """三条请求路径都要带上思考预算：普通生成、手写 agent loop 的单次请求、SDK 自动函数调用。"""
    from app.llm.base import Message, ToolSpec

    tool = ToolSpec(name="t", description="d", parameters={"type": "object", "properties": {}})

    def python_tool() -> str:
        """a tool"""
        return "x"

    async def run():
        for coroutine in (
            client.generate("hi"),
            client.generate_with_tools([Message(role="user", text="hi")], [tool]),
            client.generate_with_python_tools("hi", [python_tool]),
        ):
            with pytest.raises(Exception):  # noqa: B017  假客户端故意抛错，我们只看收到的配置
                await coroutine

    asyncio.run(run())


@pytest.mark.parametrize("budget, expected", [(None, None), (0, 0), (-1, -1), (1024, 1024)])
def test_the_thinking_budget_is_only_sent_when_configured_and_zero_counts(sink, budget, expected):
    call_all_three_paths(gemini.GeminiClient(api_key="k", model="m", thinking_budget=budget))
    assert len(sink.configs) == 3
    assert [(c.thinking_config.thinking_budget if c.thinking_config else None) for c in sink.configs] == [expected] * 3


def test_the_factory_passes_the_setting_on(sink):
    client = create_llm_client(Settings(_env_file=None, gemini_api_key="k", gemini_model="m", llm_thinking_budget=0))
    call_all_three_paths(client)
    assert all(c.thinking_config.thinking_budget == 0 for c in sink.configs)


def test_the_factory_passes_the_setting_on_the_vertex_channel_too(sink):
    client = create_llm_client(Settings(_env_file=None, llm_provider="vertex", gcp_project="p", gemini_model="m", llm_thinking_budget=0))
    call_all_three_paths(client)
    assert all(c.thinking_config.thinking_budget == 0 for c in sink.configs)


@pytest.mark.parametrize("bad", [-2, 40000])
def test_out_of_range_budgets_are_rejected_at_startup(bad):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_thinking_budget=bad)


async def collect(client, **kw):
    return [piece async for piece in client.generate_stream("hi", **kw)]


async def test_streaming_yields_text_pieces_in_order_and_skips_thoughts(sink):
    sink.holder["chunks"] = [chunk(("Hel", False)), chunk(("secret reasoning", True), ("lo", False)), chunk(("", False)), SimpleNamespace(candidates=[])]
    assert await collect(gemini.GeminiClient(api_key="k", model="m")) == ["Hel", "lo"]


async def test_stream_errors_become_llm_errors(sink):
    sink.holder["error"] = RuntimeError("boom with details")
    with pytest.raises(LLMError, match="Gemini stream failed"):
        await collect(gemini.GeminiClient(api_key="k", model="m"))


async def test_streaming_carries_the_temperature_and_system_prompt(sink):
    sink.holder["chunks"] = [chunk(("x", False))]
    await collect(gemini.GeminiClient(api_key="k", model="m", temperature=0.0), system="be brief")
    config = sink.configs[0]
    assert config.temperature == 0.0 and config.system_instruction == "be brief"


@pytest.mark.parametrize("ok", [-1, 0, 24576, 32768])
def test_the_whole_documented_budget_range_is_accepted(ok):
    assert Settings(_env_file=None, llm_thinking_budget=ok).llm_thinking_budget == ok
