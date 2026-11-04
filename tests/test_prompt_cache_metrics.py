"""提示词缓存命中量的度量测试。

背景：agent 每一圈都把完整历史重发给模型，所以输入 token 里大部分是"和上一圈一样的前缀"。
Gemini 会自动缓存这类重复前缀（命中的部分更便宜、更快）。我们【不控制】缓存，只【度量】它：
先量出命中了多少，再决定值不值得做显式缓存。这里测的是"度量本身准不准"：
  1. 从 SDK 的响应里正确读出命中数（缺失当 0，不崩）；
  2. 一轮调查里把每圈的命中数加起来；
  3. 评估汇总里算比例时，旧结果文件（没记录这一项）不能被当成"0% 命中"。
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.agent.loop import InvestigationAgent
from app.agent.tools import build_investigation_tools
from app.evals.agent_eval import CaseResult, result_from_dict, result_to_dict, summarize
from app.evals.runner import format_report
from app.llm.gemini import _to_llm_response
from app.scenario import load_scenario
from tests.fakes import GET_TICKET, SUBMIT_TOOL_NAME, VALID_REPORT_ARGS, ScriptedLLM, ToolCall, calls, final_text


def sdk_response(usage):
    part = SimpleNamespace(text="hi", thought=False, function_call=None)
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))], usage_metadata=usage)


@pytest.mark.parametrize(
    "usage, expected",
    [
        (SimpleNamespace(prompt_token_count=1000, candidates_token_count=5, cached_content_token_count=800), 800),
        (SimpleNamespace(prompt_token_count=1000, candidates_token_count=5, cached_content_token_count=None), 0),
        (None, 0),
    ],
)
def test_the_cached_token_count_is_read_from_the_sdk_response(usage, expected):
    assert _to_llm_response(sdk_response(usage), 0.0).cached_input_tokens == expected


async def test_a_turn_adds_up_the_cache_hits_of_every_model_call():
    script = [calls(GET_TICKET), calls(ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)), final_text()]
    llm = ScriptedLLM([replace(r, cached_input_tokens=n) for r, n in zip(script, (0, 7, 11), strict=True)])
    agent = InvestigationAgent(llm, build_investigation_tools(load_scenario("bug-1042")))
    result = await agent.investigate("BUG-1042")
    assert result.cached_input_tokens == 18 and result.input_tokens == 30  # 3 次调用，每次 10 个输入 token


def case(input_tokens, cached):
    return CaseResult(scenario="s", category="c", difficulty="easy", has_culprit_deploy=True, input_tokens=input_tokens, cached_input_tokens=cached)


def test_the_cached_share_is_cached_over_input_tokens():
    assert summarize([case(1000, 600), case(1000, 200)])["cost"]["cached_input_share"] == 0.4


def test_results_saved_before_this_was_recorded_are_not_counted_as_zero_percent():
    old = case(1000, None)
    assert summarize([old])["cost"]["cached_input_share"] is None  # 没记录 ≠ 0% 命中
    # 新旧混在一起：只统计记录过的，不让旧结果把比例拉低
    assert summarize([old, case(1000, 500)])["cost"]["cached_input_share"] == 0.5


def test_an_old_results_file_still_loads_with_the_field_unset():
    data = result_to_dict(case(10, 5))
    del data["cached_input_tokens"]
    assert result_from_dict(data).cached_input_tokens is None


def test_the_report_shows_the_cache_line_only_when_it_was_recorded():
    recorded = format_report(summarize([case(1000, 250)]), "t")
    assert "prompt cache: 25.0% of input tokens" in recorded
    assert "prompt cache" not in format_report(summarize([case(1000, None)]), "t")


async def test_run_case_records_the_cache_hits_of_the_run():
    import json

    from app.evals.agent_eval import run_case
    from app.evals.baselines import BASELINES
    from app.scenario import SCENARIO_DIR

    entry = json.loads((SCENARIO_DIR.parent / "eval" / "agent_scenarios.json").read_text(encoding="utf-8"))[0]
    result = await run_case(entry, BASELINES["oracle"](load_scenario(entry["scenario"])))
    assert result.cached_input_tokens == 0  # 记录过、值是 0（假模型没有缓存）；None 才是"没记录"
