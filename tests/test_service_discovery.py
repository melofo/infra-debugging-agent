"""服务目录工具（list_services）和第二版提示词（v2）的测试。

这是评估驱动的改进：评估发现 agent 不去查"别的服务"的日志（比如 config-service、secrets-manager），
猜测原因之一是它根本不知道还有这些服务。list_services + 一段通用的排查原则是对这个猜测的干预，
是否有效要看对照实验的数据，不是看这里的测试。这里只保证：改动本身正确、默认不改变现有行为、没有"针对考题作弊"。
"""

import json

import pytest

from app.agent.common import SYSTEM_PROMPT, SYSTEM_PROMPT_V2, SubmitRCAArgs
from app.agent.sdk_afc import SdkAfcAgent
from app.agent.tools import ToolError, build_investigation_tools
from app.evals.agent_eval import AGENT_VARIANTS, run_case
from app.llm.base import LLMResponse
from app.mcp_client import connect_mcp
from app.mcp_server import build_mcp_server
from app.scenario import SCENARIO_DIR, load_scenario
from tests.fakes import ScriptedLLM, ToolCall, calls, final_text, report_args, JIRA_EVIDENCE, SUBMIT_TOOL_NAME

MANIFEST = json.loads((SCENARIO_DIR.parent / "eval" / "agent_scenarios.json").read_text(encoding="utf-8"))


def test_service_discovery_is_off_by_default_so_existing_behaviour_does_not_change():
    registry = build_investigation_tools(load_scenario("bug-1042"))
    assert "list_services" not in registry.names()
    assert [s.name for s in registry.specs()] == ["get_ticket", "search_logs", "get_recent_deploys", "search_runbooks"]


async def test_list_services_returns_every_known_service_name_and_nothing_else():
    scenario = load_scenario("bug-2010")  # 功能开关：关键日志在 config-service
    registry = build_investigation_tools(scenario, service_discovery=True)
    output = await registry.execute("list_services", {})
    assert output == {"services": sorted({l.service for l in scenario.logs} | {d.service for d in scenario.deploys} | {scenario.ticket.service})}
    assert "config-service" in output["services"]
    # 只有名字：不泄露任何日志内容，更不会碰标准答案
    assert scenario.ground_truth.root_cause not in json.dumps(output)


async def test_list_services_takes_no_arguments_and_cannot_be_used_to_smuggle_any():
    registry = build_investigation_tools(load_scenario("bug-2007"), service_discovery=True)
    spec = next(s for s in registry.specs() if s.name == "list_services")
    assert spec.parameters.get("properties", {}) == {}
    assert "secrets-manager" in (await registry.execute("list_services", {}))["services"]
    with pytest.raises(ToolError, match="unknown tool"):
        await registry.execute("delete_services", {})


async def test_the_tool_works_through_mcp_and_the_default_tool_set_is_unchanged():
    scenario = load_scenario("bug-2007")
    async with connect_mcp(build_mcp_server(build_investigation_tools(scenario, service_discovery=True))) as tools:
        assert "list_services" in tools.names()
        direct = build_investigation_tools(scenario, service_discovery=True)
        assert await tools.execute("list_services", {}) == await direct.execute("list_services", {})
    async with connect_mcp(build_mcp_server(build_investigation_tools(scenario))) as tools:
        assert "list_services" not in tools.names()


class FakeAfcGemini:
    def __init__(self):
        self.functions = {}

    async def generate_with_python_tools(self, prompt, functions, system=None, max_remote_calls=8):
        self.functions = {f.__name__: f for f in functions}
        return LLMResponse(text="", input_tokens=1, output_tokens=1, latency_ms=1)


async def test_the_sdk_variant_exposes_the_tool_only_when_it_is_enabled():
    from app.agent.common import AgentError

    for enabled in (False, True):
        gemini = FakeAfcGemini()
        with pytest.raises(AgentError):  # 没提交报告会失败；这里只关心暴露了哪些函数
            await SdkAfcAgent(gemini, build_investigation_tools(load_scenario("bug-2010"), service_discovery=enabled)).investigate("BUG-2010")
        assert ("list_services" in gemini.functions) is enabled
    assert "config-service" in (await gemini.functions["list_services"]())["services"]


# ============================================================ 提示词：只加通用原则，不针对考题
def test_v2_prompt_extends_v1_without_changing_it():
    assert SYSTEM_PROMPT_V2.startswith(SYSTEM_PROMPT)
    assert "list_services" in SYSTEM_PROMPT_V2 and "list_services" not in SYSTEM_PROMPT  # v1 没有这个工具，提示词不能提它


def test_the_v2_prompt_does_not_name_any_service_or_scenario_from_the_evaluation_set():
    """防止"针对考题作弊"：提示词里不能出现评估场景里的服务名、功能开关名、密钥名，只能是通用的排查原则。"""
    addendum = SYSTEM_PROMPT_V2[len(SYSTEM_PROMPT):].lower()
    forbidden = set()
    for entry in MANIFEST:
        s = load_scenario(entry["scenario"])
        forbidden |= {l.service for l in s.logs} | {d.service for d in s.deploys} | {s.ticket.service}
        forbidden |= {a.split("|")[0] for g in s.ground_truth.must_have for a in g.split("|")}
    leaked = sorted(name for name in forbidden if len(name) > 4 and name.lower() in addendum)
    assert leaked == []


def test_the_v2_prompt_does_not_hint_at_the_held_out_incident_types():
    """留出集的意义是"调完之后检验有没有真的学会"。提示词里一旦出现留出集的题型（扩缩容、DNS、网络策略），
    留出集就不再是没见过的新题了。"""
    addendum = SYSTEM_PROMPT_V2[len(SYSTEM_PROMPT):].lower()
    terms = set()
    for entry in MANIFEST:
        if entry["split"] != "holdout":
            continue
        truth = load_scenario(entry["scenario"]).ground_truth
        terms |= {alt.strip().lower() for alt in truth.keywords[0].split("|")}  # 题型的核心概念
        terms |= {alt.strip().lower() for group in truth.must_have for alt in group.split("|")}
    terms = {t for t in terms if len(t) >= 3 and not t[0].isdigit()}
    assert terms and sorted(t for t in terms if t in addendum) == []


# ============================================================ 评估框架里的对照实验
class Recorder(ScriptedLLM):
    def __init__(self, responses):
        super().__init__(responses)
        self.tool_names, self.system = [], None

    async def generate_with_tools(self, messages, tools, system=None):
        self.tool_names, self.system = [t.name for t in tools], system
        return await super().generate_with_tools(messages, tools, system)


@pytest.mark.parametrize("variant", ["v1", "v2"])
async def test_each_variant_gives_the_model_its_own_tools_and_prompt_and_is_recorded(variant):
    entry = next(e for e in MANIFEST if e["scenario"] == "bug-2010")
    ticket = load_scenario("bug-2010").ticket
    llm = Recorder([calls(ToolCall("get_ticket", {"ticket_id": ticket.id})), calls(ToolCall(SUBMIT_TOOL_NAME, report_args(JIRA_EVIDENCE | {"reference": ticket.id}))), final_text()])
    result = await run_case(entry, llm, variant=variant, max_steps=11)

    config = AGENT_VARIANTS[variant]
    assert ("list_services" in llm.tool_names) is config.service_discovery
    assert llm.system == config.prompt
    assert (result.variant, result.max_steps) == (variant, 11)  # 结果里记下了"是在什么条件下测的"


def test_the_cli_accepts_the_agent_variant_and_states_it(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "eval_agent.py"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    out = subprocess.run([sys.executable, str(script), "--llm", "oracle", "--agent", "v2", "--max-steps", "12", "--split", "holdout", "--out", str(tmp_path / "o.jsonl")],
                         cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "agent v2, max steps 12" in out.stdout and "6 runs" in out.stdout
    rows = [json.loads(line) for line in (tmp_path / "o.jsonl").read_text().splitlines()]
    assert {(r["variant"], r["max_steps"]) for r in rows} == {("v2", 12)}
