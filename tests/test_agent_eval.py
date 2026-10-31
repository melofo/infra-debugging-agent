"""端到端评估框架的测试：评分、统计、批量运行、续跑、命令行。

最重要的一组是"参照线"测试：用不花钱的替身在整个数据集上跑一遍，要求
  oracle 100%（题目都有解，评分器和数据集自洽）、
  "怪最近一次发布"、"抄 runbook"、"复述工单"都很低（题目不能靠投机取巧过关）。
以后谁改了数据、评分规则或证据检查，导致其中一条线变形，这里会立刻报警。
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.agent.common import AgentError
from app.evals.agent_eval import CaseResult, count_rejections, run_case, summarize, wilson_interval
from app.evals.baselines import BASELINES, Oracle
from app.evals.runner import format_report, load_results, run_all, select_cases
from app.evals.scoring import calibration_gap, cites_culprit, correct_rank, percentile
from app.llm.base import LLMError, ToolCall
from app.scenario import SCENARIO_DIR, load_scenario
from app.schemas import RCAReport
from tests.fakes import SUBMIT_TOOL_NAME, ScriptedLLM, calls, final_text, report_args

MANIFEST = json.loads((SCENARIO_DIR.parent / "eval" / "agent_scenarios.json").read_text(encoding="utf-8"))
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_agent.py"


async def run_baseline(name: str) -> list[CaseResult]:
    return [await run_case(e, BASELINES[name](load_scenario(e["scenario"]))) for e in MANIFEST]


# ============================================================ 参照线：评估本身是否可信
async def test_the_oracle_gets_every_case_right_so_every_case_is_answerable_and_groundable():
    results = await run_baseline("oracle")
    failed = [(r.scenario, r.status, r.top_cause) for r in results if not r.top1]
    assert failed == []
    assert all(r.cites_culprit and r.grounding_rejections == 0 for r in results)


async def test_naive_strategies_score_low():
    latest = summarize(await run_baseline("latest_deploy"))
    assert latest["top1"]["rate"] <= 0.25  # 只会怪最近一次发布：一半的故障不是发布引起的，困难题里最近的发布是无辜的
    assert latest["by_culprit_deploy"]["no culprit deploy"]["top1"] == 0.0
    assert latest["by_difficulty"]["hard"]["top1"] == 0.0
    assert summarize(await run_baseline("runbook_copy"))["top1"]["rate"] == 0.0  # 整段抄 runbook 抄不出特有的事实
    assert summarize(await run_baseline("ticket_echo"))["top1"]["rate"] == 0.0


# ============================================================ 评分
def make_report(*causes: str, evidence_refs=("BUG-1",)) -> RCAReport:
    hypotheses = [
        {"root_cause": c, "confidence": 0.9 - i * 0.1, "evidence": [{"source": "jira", "summary": "s", "reference": r} for r in evidence_refs]}
        for i, c in enumerate(causes)
    ]
    return RCAReport(incident_id="BUG-1", summary="s", hypotheses=hypotheses)


def test_correct_rank_is_the_position_of_the_first_correct_hypothesis():
    keywords, must = ["pool", "exhaust"], ["maximumpoolsize"]
    report = make_report("the database is slow", "pool exhausted: maximumPoolSize reduced", "something else")
    assert correct_rank(report, keywords, must) == 2
    assert correct_rank(make_report("nothing relevant"), keywords, must) is None
    assert correct_rank(make_report("pool exhausted but no specifics"), keywords, must) is None  # 缺特有的事实


def test_the_next_steps_count_as_part_of_the_claim_but_evidence_does_not():
    keywords, must = ["pool", "index"], ["maximumpoolsize"]
    report = RCAReport(incident_id="B", summary="s", hypotheses=[{
        "root_cause": "x", "confidence": 0.5, "suggested_next_steps": ["raise maximumPoolSize, check the pool and index"],
        "evidence": [{"source": "jira", "summary": "pool index maximumPoolSize", "reference": "BUG-1"}],
    }])
    assert correct_rank(report, keywords, must) == 1
    only_evidence = RCAReport(incident_id="B", summary="s", hypotheses=[{
        "root_cause": "x", "confidence": 0.5, "evidence": [{"source": "jira", "summary": "pool index maximumPoolSize", "reference": "BUG-1"}],
    }])
    assert correct_rank(only_evidence, keywords, must) is None


def test_cites_culprit_matches_any_reference_case_insensitively():
    report = make_report("x", evidence_refs=("commit 7BE41D0 reduced it",))
    assert cites_culprit(report, ["7be41d0"]) and not cites_culprit(report, ["deadbee"])


def test_calibration_and_percentile_helpers():
    assert calibration_gap([(0.9, True), (0.9, False)]) == 0.4  # 平均置信度 0.9，准确率 0.5
    assert calibration_gap([]) is None
    assert percentile([10, 20, 30, 40, 50], 0.5) == 30 and percentile([10, 20, 30, 40, 50], 0.95) == 50 and percentile([], 0.5) is None


def test_wilson_interval_known_values():
    assert wilson_interval(0, 10) == (0.0, 0.2775)
    assert wilson_interval(10, 10) == (0.7225, 1.0)
    low, high = wilson_interval(5, 10)
    assert (round(low, 3), round(high, 3)) == (0.237, 0.763)
    assert wilson_interval(0, 0) == (0.0, 0.0)


# ============================================================ 统计
def result(scenario="a", category="c1", difficulty="easy", culprit=True, **kw) -> CaseResult:
    base = dict(scenario=scenario, category=category, difficulty=difficulty, has_culprit_deploy=culprit, tool_calls=3, llm_calls=5,
                input_tokens=1000, output_tokens=100, latency_ms=2000, top_confidence=0.8)
    base.update(kw)
    return CaseResult(**base)


def test_errors_stay_in_the_denominator_and_count_as_wrong():
    results = [
        result("a", top1=True, top3=True, correct_rank=1),
        result("b", top1=False, top3=True, correct_rank=2),
        result("c", status="llm_error", top_confidence=None, llm_calls=0, input_tokens=0, output_tokens=0),
        result("d"),
    ]
    s = summarize(results)
    assert (s["top1"]["count"], s["top1"]["n"]) == (1, 4) and (s["top3"]["count"], s["top3"]["n"]) == (2, 4)
    assert s["errors"]["count"] == 1 and s["error_kinds"] == {"llm_error": 1}
    assert s["cost"]["avg_llm_calls"] == 5  # 成本只在成功的案例上平均


def test_grouping_calibration_cost_and_instability():
    results = [
        result("a", "c1", "easy", True, top1=True, top3=True, top_confidence=0.9, repeat=0),
        result("a", "c1", "easy", True, top1=False, top3=False, top_confidence=0.9, repeat=1),
        result("b", "c2", "hard", False, top1=False, top3=False, top_confidence=0.7),
    ]
    s = summarize(results, price_in_per_million=1.0, price_out_per_million=2.0)
    assert s["by_category"]["c1"]["top1"] == 0.5 and s["by_category"]["c2"]["top1"] == 0.0
    assert s["by_difficulty"]["hard"]["n"] == 1 and s["by_culprit_deploy"]["no culprit deploy"]["n"] == 1
    assert s["unstable_cases"] == ["a"]  # 同一个案例时对时错
    assert s["calibration_gap"] == round((0.9 + 0.9 + 0.7) / 3 - 1 / 3, 4)
    assert s["cost"]["estimated_cost_usd"] == round((3000 * 1.0 + 300 * 2.0) / 1e6, 4)
    assert summarize(results)["cost"]["estimated_cost_usd"] is None  # 没给单价就不估算


def test_the_text_report_shows_the_headline_numbers():
    text = format_report(summarize([result("a", top1=True, top3=True), result("b")]), "demo")
    assert "Top-1 accuracy" in text and "50.0%" in text and "95% CI" in text and "by category" in text


# ============================================================ run_case：出错也要留下一条"答错"的记录
ENTRY = MANIFEST[0]


async def test_a_failing_model_becomes_a_wrong_answer_not_a_crash():
    class Broken(ScriptedLLM):
        async def generate_with_tools(self, messages, tools, system=None):
            raise LLMError("quota exhausted")

    r = await run_case(ENTRY, Broken([]))
    assert (r.status, r.top1, r.top3) == ("llm_error", False, False) and "quota" in r.error


async def test_a_model_that_never_submits_is_an_agent_error():
    from app.llm.base import ToolCall as TC

    r = await run_case(ENTRY, ScriptedLLM([calls(TC("get_ticket", {"ticket_id": ENTRY["scenario"].upper()}))] * 20), max_steps=3)
    assert r.status == "agent_error"


async def test_a_hanging_model_times_out():
    class Hangs(ScriptedLLM):
        async def generate_with_tools(self, messages, tools, system=None):
            await asyncio.sleep(3600)

    r = await run_case(ENTRY, Hangs([]), timeout_seconds=0.1)
    assert r.status == "timeout" and not r.top1


async def test_rejections_are_counted_separately_and_do_not_stop_a_correct_answer():
    scenario = load_scenario(ENTRY["scenario"])
    ticket = scenario.ticket
    good = Oracle(scenario)  # 用 oracle 的正确答案，但前面先插入两次被打回的提交
    fabricated = report_args({"source": "git", "summary": "s", "reference": "abc1234"})
    invalid = {"summary": "s", "hypotheses": [{"root_cause": "x", "confidence": 0.5, "evidence": []}]}

    class Sloppy:
        def __init__(self):
            self.stage = 0

        async def generate(self, *a, **k):
            raise NotImplementedError

        async def generate_with_tools(self, messages, tools, system=None):
            last = messages[-1]
            if last.role == "user" and self.stage == 0:
                self.stage = 1
                return calls(ToolCall("get_ticket", {"ticket_id": ticket.id}))
            if last.role == "tool" and last.tool_results[0].name == "get_ticket" and self.stage == 1:
                self.stage = 2
                return calls(ToolCall(SUBMIT_TOOL_NAME, fabricated))
            if last.role == "tool" and self.stage == 2:
                self.stage = 3
                return calls(ToolCall(SUBMIT_TOOL_NAME, invalid))
            return await good.generate_with_tools(messages, tools, system)

    r = await run_case(ENTRY, Sloppy())
    assert (r.grounding_rejections, r.schema_rejections) == (1, 1)
    assert r.top1 and r.status == "ok"  # 被打回、改正之后，照常打分


def test_count_rejections_reads_the_step_previews():
    from app.agent.common import StepRecord

    steps = [
        StepRecord(1, SUBMIT_TOOL_NAME, {}, False, "report rejected: some evidence could not be verified"),
        StepRecord(2, SUBMIT_TOOL_NAME, {}, False, "invalid report: [...]"),
        StepRecord(3, "search_logs", {}, False, "report rejected is just text in a tool result"),
        StepRecord(4, SUBMIT_TOOL_NAME, {}, True, "report submitted"),
    ]
    assert count_rejections(steps) == (1, 1)


# ============================================================ 批量运行：边跑边写、续跑、重复
def oracle_factory(entry):
    return Oracle(load_scenario(entry["scenario"]))


async def test_results_are_written_as_each_case_finishes_and_a_rerun_only_does_the_rest(tmp_path):
    out = tmp_path / "run.jsonl"
    cases = MANIFEST[:4]
    seen_lines = []
    await run_all(cases[:2], oracle_factory, out, on_result=lambda r: seen_lines.append(len(out.read_text().splitlines())))
    assert seen_lines == [1, 2]  # 每个案例做完，文件里就多一行（崩溃了也不会丢）

    invoked = []

    def counting_factory(entry):
        invoked.append(entry["scenario"])
        return oracle_factory(entry)

    results = await run_all(cases, counting_factory, out)
    assert invoked == [c["scenario"] for c in cases[2:]]  # 前两个已经做完，不再重做
    assert [r.scenario for r in results] == [c["scenario"] for c in cases]
    assert len(load_results(out)) == 4


async def test_repeat_runs_each_case_several_times_with_distinct_indices(tmp_path):
    results = await run_all(MANIFEST[:2], oracle_factory, tmp_path / "r.jsonl", repeat=3)
    assert sorted((r.scenario, r.repeat) for r in results) == sorted((c["scenario"], i) for c in MANIFEST[:2] for i in range(3))


async def test_results_only_include_the_requested_cases_even_if_the_file_has_more(tmp_path):
    out = tmp_path / "r.jsonl"
    await run_all(MANIFEST[:3], oracle_factory, out)
    results = await run_all(MANIFEST[:1], oracle_factory, out)
    assert [r.scenario for r in results] == [MANIFEST[0]["scenario"]]


def test_select_cases_filters():
    assert len(select_cases(MANIFEST, None, None, None, None)) == 36  # 默认 split="all"
    assert len(select_cases(MANIFEST, None, None, None, None, split="dev")) == 30
    assert {e["split"] for e in select_cases(MANIFEST, None, None, None, None, split="holdout")} == {"holdout"}
    assert {e["difficulty"] for e in select_cases(MANIFEST, None, None, "hard", None)} == {"hard"}
    assert len(select_cases(MANIFEST, None, "expired_certificate", None, None)) == 3
    assert [e["scenario"] for e in select_cases(MANIFEST, ["bug-2003"], None, None, None)] == ["bug-2003"]
    assert len(select_cases(MANIFEST, None, None, None, 5)) == 5


# ============================================================ 命令行
def run_cli(args, cwd, **env):
    environment = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "GEMINI_API_KEY", "GEMINI_MODEL")}
    environment.update(env)
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, env=environment, capture_output=True, text=True, timeout=120)


def test_the_cli_runs_a_baseline_from_any_directory_without_pythonpath(tmp_path):
    result = run_cli(["--llm", "oracle", "--limit", "3", "--out", str(tmp_path / "o.jsonl")], tmp_path)
    assert result.returncode == 0, result.stderr
    assert "Top-1 accuracy" in result.stdout and "100.0%" in result.stdout
    assert len((tmp_path / "o.jsonl").read_text().splitlines()) == 3


def test_the_cli_refuses_the_real_model_without_configuration_and_spends_nothing(tmp_path):
    result = run_cli(["--llm", "gemini", "--limit", "1", "--out", str(tmp_path / "g.jsonl")], tmp_path)
    assert result.returncode == 2 and "GEMINI_API_KEY" in result.stdout
    assert "Traceback" not in result.stderr and not (tmp_path / "g.jsonl").exists()


def test_the_cli_rejects_filters_that_match_nothing(tmp_path):
    result = run_cli(["--llm", "oracle", "--category", "nope", "--out", str(tmp_path / "x.jsonl")], tmp_path)
    assert result.returncode == 2 and "no cases" in result.stdout


class WrongThenRight(Oracle):
    """排第一的假设是错的（置信度更高），正确的排第二：应该是 Top-3 对、Top-1 错。"""

    def final_report(self, outputs):
        report = super().final_report(outputs)
        wrong = {"root_cause": "The database host ran out of memory", "confidence": 0.95, "evidence": report["hypotheses"][0]["evidence"], "suggested_next_steps": []}
        report["hypotheses"][0]["confidence"] = 0.5
        report["hypotheses"].insert(0, wrong)
        return report


async def test_a_correct_answer_ranked_second_is_top3_but_not_top1():
    r = await run_case(ENTRY, WrongThenRight(load_scenario(ENTRY["scenario"])))
    assert (r.correct_rank, r.top1, r.top3) == (2, False, True)
    assert r.top_confidence == 0.95  # 置信度记录的是排第一的那个


async def test_citing_the_key_evidence_is_measured_separately_from_being_right():
    echo = await run_case(ENTRY, BASELINES["ticket_echo"](load_scenario(ENTRY["scenario"])))
    assert echo.status == "ok" and echo.cites_culprit is False  # 只引用了工单号，没有引用关键出处
    oracle = await run_case(ENTRY, Oracle(load_scenario(ENTRY["scenario"])))
    assert oracle.cites_culprit is True


# ============================================================ 对已保存的结果重新打分
async def test_rescoring_uses_the_full_report_and_changes_only_what_the_rule_changes(tmp_path):
    from app.evals.runner import rescore

    out = tmp_path / "r.jsonl"
    results = await run_all(MANIFEST[:3], oracle_factory, out)
    assert all(r.report is not None for r in results)  # 完整报告被保存了

    same, approximate = rescore(results)
    assert approximate == 0 and [r.top1 for r in same] == [r.top1 for r in results]

    # 把一个案例的保存结果改成"答非所问"，重新打分应该判错；其他的不受影响
    tampered = [CaseResult(**{**r.__dict__}) for r in results]
    tampered[0].report["hypotheses"][0]["root_cause"] = "The database host ran out of memory"
    tampered[0].report["hypotheses"][0]["suggested_next_steps"] = []
    rescored, _ = rescore(tampered)
    assert [r.top1 for r in rescored] == [False, True, True]


def test_results_saved_without_a_report_are_rescored_approximately_and_the_caller_is_told():
    from app.evals.runner import rescore

    entry = MANIFEST[0]
    truth = load_scenario(entry["scenario"]).ground_truth
    old = CaseResult(scenario=entry["scenario"], category=entry["category"], difficulty=entry["difficulty"],
                     has_culprit_deploy=True, top1=False, top3=False, top_cause=truth.root_cause)
    [new], approximate = rescore([old])
    assert approximate == 1 and new.top1 is True  # 标准答案的文字按当前规则必判对
    errored = CaseResult(scenario=entry["scenario"], category=entry["category"], difficulty=entry["difficulty"],
                         has_culprit_deploy=True, status="timeout")
    assert rescore([errored])[0][0].top1 is False  # 出错的案例不会因为重新打分变成"对"


def test_the_cli_can_rescore_a_saved_file_without_any_model(tmp_path):
    out = tmp_path / "saved.jsonl"
    first = run_cli(["--llm", "oracle", "--limit", "3", "--out", str(out)], tmp_path)
    assert first.returncode == 0
    result = run_cli(["--rescore", str(out)], tmp_path)
    assert result.returncode == 0, result.stderr
    assert "no model calls" in result.stdout and "cases whose verdict changed: none" in result.stdout
    assert run_cli(["--rescore", str(tmp_path / "missing.jsonl")], tmp_path).returncode == 2


async def test_failed_runs_keep_the_steps_they_took_so_the_failure_can_be_analysed():
    from app.llm.base import ToolCall as TC

    scenario = load_scenario(ENTRY["scenario"])
    loop_forever = ScriptedLLM([calls(TC("get_ticket", {"ticket_id": scenario.ticket.id})), calls(TC("search_logs", {"service": scenario.ticket.service})), *([calls(TC("get_recent_deploys", {"service": scenario.ticket.service}))] * 6)])
    r = await run_case(ENTRY, loop_forever, max_steps=4)
    assert r.status == "agent_error"
    assert r.steps_taken == 4 and r.log_services == [scenario.ticket.service]
    assert r.tools_used == ["get_recent_deploys", "get_ticket", "search_logs"]


async def test_cross_service_lookup_is_measured():
    cross = next(e for e in MANIFEST if load_scenario(e["scenario"]).ground_truth.category == "credential_rotation")
    scenario = load_scenario(cross["scenario"])
    lazy = await run_case(cross, BASELINES["latest_deploy"](scenario))  # 只查工单所属服务，不会去 secrets-manager
    thorough = await run_case(cross, Oracle(scenario))
    assert lazy.cross_service and lazy.key_service_queried is False
    assert thorough.cross_service and thorough.key_service_queried is True
    summary = summarize([lazy, thorough])
    assert summary["looked_in_the_key_service_when_it_was_another_service"]["rate"] == 0.5
    assert summary["accuracy_when_it_looked_in_the_key_service"]["rate"] == 1.0
    assert summary["accuracy_when_it_did_not"]["rate"] == 0.0
