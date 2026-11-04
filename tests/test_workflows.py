"""路由、并行、串联三种工作流的测试（假模型，不联网、不花钱）。

要证明的不是"模型答得对"（那要用真模型评估），而是流程本身的性质：
  路由：标签只能是预先列好的几个，其他任何回答都退回 general；只跑被选中的那一位专家。
  并行：专家真的【同时】在跑（不是一个接一个）；一位失败不连累其他人；意外的异常不被悄悄吞掉；合并后有序且有上限。
  串联：有把握就停在第一步；没把握才升级，升级后的报告包含更多专家的假设；token 账目要算全。
"""

import asyncio
import json

import pytest

from app.agent.common import SYSTEM_PROMPT_V2, AgentError
from app.agent.tools import build_investigation_tools
from app.agent.workflows import (
    GENERAL,
    ROUTER_SYSTEM,
    SPECIALISTS,
    investigate_chained,
    investigate_parallel,
    investigate_routed,
    merge_reports,
    route_ticket,
    specialist_prompt,
)
from app.llm.base import LLMError, LLMResponse
from app.scenario import SCENARIO_DIR, load_scenario
from tests.fakes import JIRA_EVIDENCE, SUBMIT_TOOL_NAME, ToolCall, calls, final_text, report_args

TICKET = "BUG-1042"


def make_registry():
    return build_investigation_tools(load_scenario("bug-1042"), service_discovery=True)


class FakeLLM:
    """无状态的假模型（并发安全）：从系统提示词里认出自己是哪位专家，按配置给出置信度或失败。"""

    def __init__(self, router="config", confidence=None, delay=0.0, fail=(), crash=()):
        self.router = router
        self.confidence = confidence or {}
        self.delay = delay
        self.fail, self.crash = set(fail), set(crash)
        self.systems: list[str] = []
        self.router_calls = 0
        self.in_flight = 0
        self.max_in_flight = 0

    def role(self, system: str) -> str:
        return next((name for name, focus in SPECIALISTS.items() if focus in (system or "")), GENERAL)

    async def generate(self, prompt, system=None):
        self.router_calls += 1
        if isinstance(self.router, Exception):
            raise self.router
        return LLMResponse(text=self.router, input_tokens=7, output_tokens=2, latency_ms=1)

    async def generate_with_tools(self, messages, tools, system=None):
        role = self.role(system)
        self.systems.append(system)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            if role in self.fail:
                raise LLMError(f"{role} unavailable")
            if role in self.crash:
                raise RuntimeError("a bug, not an expected failure")
            last = messages[-1]
            if last.role == "user" and "Finish by calling" not in last.text:
                return calls(ToolCall("get_ticket", {"ticket_id": TICKET}))
            if last.role == "tool" and last.tool_results[0].name == "get_ticket":
                args = report_args(JIRA_EVIDENCE)
                args["summary"] = f"summary from the {role} specialist"
                args["hypotheses"][0]["root_cause"] = f"cause found by the {role} specialist"
                args["hypotheses"][0]["confidence"] = self.confidence.get(role, 0.9)
                return calls(ToolCall(SUBMIT_TOOL_NAME, args))
            return final_text("Done.")
        finally:
            self.in_flight -= 1


# ---------------------------------------------------------------- 路由
@pytest.mark.parametrize(
    "answer, expected",
    [
        ("config", "config"),
        (" Infrastructure.", "infrastructure"),
        ("deploy\n", "deploy"),
        ("`config`", "config"),
        ("I think it is probably a deploy problem", GENERAL),  # 第一个词不是标签：退回 general
        ("rm -rf /", GENERAL),  # 任意文字都不会被当成别的东西
        ("", GENERAL),
        ("unknown", GENERAL),
    ],
)
async def test_the_router_only_accepts_listed_labels(answer, expected):
    label, _, _ = await route_ticket(FakeLLM(router=answer), "ticket text")
    assert label == expected


async def test_a_failing_classifier_falls_back_to_general_instead_of_failing_the_investigation():
    assert await route_ticket(FakeLLM(router=LLMError("429")), "text") == (GENERAL, 0, 0)


def test_the_general_route_is_the_plain_v2_prompt_and_specialists_only_add_one_focus_line():
    assert specialist_prompt(GENERAL) == SYSTEM_PROMPT_V2
    for name, focus in SPECIALISTS.items():
        assert specialist_prompt(name) == f"{SYSTEM_PROMPT_V2}\n\n{focus}"


def test_the_workflow_prompts_do_not_name_the_held_out_incident_types():
    manifest = json.loads((SCENARIO_DIR.parent / "eval" / "agent_scenarios.json").read_text(encoding="utf-8"))
    held_out = {w for e in manifest if e.get("split") == "holdout" for w in e["category"].split("_")} | {"dns", "autoscaler", "network", "policy"}
    text = (ROUTER_SYSTEM + " ".join(SPECIALISTS.values())).lower()
    assert [w for w in held_out if len(w) > 3 and w in text and w not in SYSTEM_PROMPT_V2.lower()] == []


async def test_routing_runs_only_the_chosen_specialist():
    llm = FakeLLM(router="config")
    result = await investigate_routed(llm, make_registry, TICKET, "ticket text")
    assert result.routes == ["config"]
    assert {llm.role(s) for s in llm.systems} == {"config"}  # 其他专家一次都没被调用
    assert result.report.hypotheses[0].root_cause == "cause found by the config specialist"


async def test_routing_counts_the_classifier_call_and_tokens():
    result = await investigate_routed(FakeLLM(), make_registry, TICKET, "text")
    # 专家 3 次调用（查工单、提交、收尾）每次 10/5 token，加上分类 1 次 7/2
    assert (result.llm_calls, result.input_tokens, result.output_tokens) == (4, 37, 17)


# ---------------------------------------------------------------- 并行
async def test_the_specialists_really_run_at_the_same_time():
    llm = FakeLLM(delay=0.05)
    await investigate_parallel(llm, make_registry, TICKET)
    assert llm.max_in_flight == len(SPECIALISTS)  # 一个接一个跑的话，同一时刻只会有 1 个


async def test_parallel_merges_hypotheses_sorted_by_confidence_and_takes_the_summary_of_the_best():
    llm = FakeLLM(confidence={"deploy": 0.3, "config": 0.8, "infrastructure": 0.5})
    result = await investigate_parallel(llm, make_registry, TICKET)
    causes = [h.root_cause for h in result.report.hypotheses]
    assert causes == [f"cause found by the {r} specialist" for r in ("config", "infrastructure", "deploy")]
    assert result.report.summary == "summary from the config specialist"  # 不是第一份，而是最有把握的那份
    assert result.routes == list(SPECIALISTS)
    assert result.llm_calls == 9 and result.input_tokens == 90 and result.output_tokens == 45  # 3 位专家 x 3 次


async def test_one_failed_specialist_does_not_sink_the_others_and_is_reported():
    result = await investigate_parallel(FakeLLM(fail={"config"}), make_registry, TICKET)
    assert result.routes == ["deploy", "infrastructure"]
    assert any("config" in note and "LLMError" in note for note in result.notes)


async def test_if_every_specialist_fails_the_workflow_fails():
    with pytest.raises(AgentError, match="no specialist produced a report"):
        await investigate_parallel(FakeLLM(fail=set(SPECIALISTS)), make_registry, TICKET)


async def test_an_unexpected_exception_is_not_swallowed():
    with pytest.raises(RuntimeError, match="a bug"):
        await investigate_parallel(FakeLLM(crash={"deploy"}), make_registry, TICKET)


async def test_merge_keeps_at_most_five_hypotheses():
    def report(n):
        args = report_args(JIRA_EVIDENCE)
        args["hypotheses"] = [{**args["hypotheses"][0], "confidence": 0.1 * (i + n)} for i in range(1, 4)]
        from app.agent.common import SubmitRCAArgs, build_report

        return build_report(TICKET, SubmitRCAArgs.model_validate(args))

    merged = merge_reports(TICKET, [report(0), report(3)])
    confidences = [h.confidence for h in merged.hypotheses]
    # 6 个假设（0.1..0.3 和 0.4..0.6）留 5 个：被丢掉的必须是【最没把握的】那个，不是别的
    assert confidences == pytest.approx([0.6, 0.5, 0.4, 0.3, 0.2])


# ---------------------------------------------------------------- 串联
async def test_a_confident_first_step_stops_there_and_does_not_pay_for_the_wide_search():
    llm = FakeLLM(router="config", confidence={"config": 0.9})
    result = await investigate_chained(llm, make_registry, TICKET, "text")
    assert result.escalated is False and result.routes == ["config"]
    assert {llm.role(s) for s in llm.systems} == {"config"}


async def test_a_low_confidence_first_step_escalates_to_all_specialists():
    llm = FakeLLM(router="config", confidence={"config": 0.4, "deploy": 0.7, "infrastructure": 0.6})
    result = await investigate_chained(llm, make_registry, TICKET, "text")
    assert result.escalated is True
    assert result.routes == ["config", *SPECIALISTS]
    assert result.report.hypotheses[0].root_cause == "cause found by the deploy specialist"  # 升级后别的专家更有把握，排到前面
    assert any(note.startswith("escalated") for note in result.notes)
    # 账目 = 第一步（分类 + 一位专家）+ 并行三位专家
    assert result.llm_calls == 4 + 9 and result.input_tokens == 37 + 90 and result.output_tokens == 17 + 45


async def test_the_gate_threshold_is_inclusive_at_the_boundary():
    llm = FakeLLM(router="config", confidence={"config": 0.6})
    result = await investigate_chained(llm, make_registry, TICKET, "text")
    assert result.escalated is False  # 恰好等于阈值：通过
