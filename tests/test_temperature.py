"""采样温度（temperature）的测试。

temperature 是模型每生成一个 token 时"怎么从候选里选"的旋钮：0 = 几乎总选概率最高的（稳定、可复现），越高越随机。
我们要保证三件事：
  1. 不配置时行为和以前完全一样（不往请求里放 temperature）；
  2. 配置成 0 时【真的传过去了】（0 在 Python 里是"假值"，写成 `if temperature:` 就会悄悄丢掉它）；
  3. 评估结果文件会记下用了多少温度，旧结果文件照样能读（类似数据库加了一个可空的新列）。
不联网：把 SDK 的客户端换成"记录员"，只看我们传给它的配置。
"""

import asyncio

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.evals.agent_eval import CaseResult, result_from_dict, result_to_dict
from app.llm import gemini
from app.llm.base import Message, ToolSpec
from app.llm.factory import create_llm_client


class FakeModels:
    def __init__(self, sink: list):
        self._sink = sink

    async def generate_content(self, *, model, contents, config):
        self._sink.append(config)
        raise RuntimeError("stop here: we only wanted to see the config")


class FakeClient:
    configs: list = []

    def __init__(self, **kwargs):
        self.aio = type("Aio", (), {"models": FakeModels(FakeClient.configs)})()


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch):
    FakeClient.configs = []
    monkeypatch.setattr(gemini.genai, "Client", FakeClient)
    return FakeClient


def call_all_three_paths(client: gemini.GeminiClient) -> list:
    """三条调用路径都要带上温度：普通生成、手写 agent loop 的单次请求、SDK 自动函数调用。"""
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
            with pytest.raises(Exception):
                await coroutine

    asyncio.run(run())
    return FakeClient.configs


def test_without_a_temperature_nothing_is_sent_so_the_model_default_applies():
    configs = call_all_three_paths(gemini.GeminiClient(api_key="k", model="m"))
    assert len(configs) == 3 and all(c.temperature is None for c in configs)


@pytest.mark.parametrize("value", [0.0, 0.7])
def test_a_temperature_reaches_every_request_path_including_zero(value):
    configs = call_all_three_paths(gemini.GeminiClient(api_key="k", model="m", temperature=value))
    assert len(configs) == 3 and all(c.temperature == value for c in configs)


@pytest.mark.parametrize("provider", ["gemini-api", "vertex"])
def test_the_factory_passes_the_setting_on_both_channels(provider):
    extra = {"gcp_project": "p"} if provider == "vertex" else {"gemini_api_key": "k"}
    client = create_llm_client(Settings(_env_file=None, llm_provider=provider, gemini_model="m", llm_temperature=0, **extra))
    configs = call_all_three_paths(client)
    assert all(c.temperature == 0 for c in configs)


def test_the_setting_defaults_to_unset():
    assert Settings(_env_file=None).llm_temperature is None


@pytest.mark.parametrize("bad", [-0.1, 2.1])
def test_out_of_range_temperatures_are_rejected_at_startup(bad):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_temperature=bad)


def test_evaluation_results_record_the_temperature_and_old_files_still_load():
    result = CaseResult(scenario="bug-2001", category="c", difficulty="easy", has_culprit_deploy=True, temperature=0.0)
    assert result_from_dict(result_to_dict(result)).temperature == 0.0  # 0 不能被读成"没设置"
    old = result_to_dict(result)
    del old["temperature"]  # 加这个字段之前保存的结果文件
    assert result_from_dict(old).temperature is None


@pytest.mark.parametrize("ok", [0.0, 1.5, 2.0])
def test_the_whole_documented_range_is_accepted(ok):
    assert Settings(_env_file=None, llm_temperature=ok).llm_temperature == ok


@pytest.mark.parametrize("given", [None, 0.0, 0.4])
def test_run_case_records_the_temperature_it_was_run_with(given):
    import json

    from app.evals.agent_eval import run_case
    from app.evals.baselines import BASELINES
    from app.scenario import SCENARIO_DIR, load_scenario

    entry = json.loads((SCENARIO_DIR.parent / "eval" / "agent_scenarios.json").read_text(encoding="utf-8"))[0]
    result = asyncio.run(run_case(entry, BASELINES["oracle"](load_scenario(entry["scenario"])), temperature=given))
    assert result.temperature == given
