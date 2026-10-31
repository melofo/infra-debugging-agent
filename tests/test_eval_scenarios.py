"""评估数据集本身的质量测试。

评估的结论只有在"题目出得好"的前提下才可信：题目有泄露答案的、答案找不到的、评分规则分不清类别的，
算出来的准确率都没有意义。所以先把"题目"和"评分器"当作被测对象。
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from app.evals.scenarios import ARCHETYPES, build_all
from app.evals.scoring import matched_groups, required_matches, text_is_correct
from app.scenario import SCENARIO_DIR, load_scenario

MANIFEST = json.loads((SCENARIO_DIR.parent / "eval" / "agent_scenarios.json").read_text(encoding="utf-8"))
NAMES = [entry["scenario"] for entry in MANIFEST]


def when(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@pytest.fixture(scope="module")
def scenarios():
    return {name: load_scenario(name) for name in NAMES}


# ============================================================ 结构
def test_there_are_thirty_dev_scenarios_covering_every_category_at_every_difficulty():
    dev = [e for e in MANIFEST if e["split"] == "dev"]
    assert len(dev) == 30 and len(NAMES) == 36 == len(set(NAMES))
    assert {(e["category"], e["difficulty"]) for e in dev} == {
        (category, level) for _, category, _, _ in ARCHETYPES for level in ("easy", "medium", "hard")
    }
    assert len([e for e in dev if not e["has_culprit_deploy"]]) == 15  # 一半的故障不是发布引起的：只会"怪最近一次发布"的 agent 过不了


def test_the_holdout_set_is_made_of_unseen_incident_types_with_no_culprit_deploy():
    holdout = [e for e in MANIFEST if e["split"] == "holdout"]
    dev_categories = {e["category"] for e in MANIFEST if e["split"] == "dev"}
    assert len(holdout) == 6 and not any(e["has_culprit_deploy"] for e in holdout)
    assert {e["category"] for e in holdout}.isdisjoint(dev_categories)  # 题型是开发集里没出现过的
    assert {e["difficulty"] for e in holdout} == {"medium", "hard"}


def test_runbook_ids_are_unique_per_archetype(scenarios):
    ids = {}
    for entry in MANIFEST:
        own = next(r.id for r in scenarios[entry["scenario"]].runbooks if r.id.startswith("RB-3"))
        ids.setdefault(own, set()).add(entry["category"])
    assert all(len(categories) == 1 for categories in ids.values())


def test_committed_files_match_the_generator():
    scenarios, manifest = build_all()
    for name, data in scenarios.items():
        on_disk = json.loads((SCENARIO_DIR / f"{name}.json").read_text(encoding="utf-8"))
        assert on_disk == data, f"{name} was edited by hand or the generator changed; run scripts/build_eval_scenarios.py"
    assert manifest == MANIFEST


def test_the_check_command_agrees():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "build_eval_scenarios.py"), "--check"], capture_output=True, text=True, cwd="/"
    )
    assert result.returncode == 0, result.stdout


def test_ids_match_file_names_and_the_manifest(scenarios):
    for entry in MANIFEST:
        scenario = scenarios[entry["scenario"]]
        assert scenario.ticket.id.lower() == entry["scenario"]
        assert scenario.ground_truth.category == entry["category"]
        assert (len(scenario.deploys) > 0) and any(r.id.startswith("RB-3") for r in scenario.runbooks)


# ============================================================ 时间线是合理的
def test_the_timeline_is_consistent(scenarios):
    for name, s in scenarios.items():
        reported = s.ticket.reported_at
        first_error = min(l.timestamp for l in s.logs if l.level == "ERROR")
        assert first_error < reported, name  # 先出故障，后有人报
        assert all(d.timestamp < reported for d in s.deploys), name


def test_a_culprit_deploy_comes_before_the_first_error(scenarios):
    for entry in MANIFEST:
        if not entry["has_culprit_deploy"]:
            continue
        s = scenarios[entry["scenario"]]
        first_error = min(l.timestamp for l in s.logs if l.level == "ERROR")
        sha = s.ground_truth.culprit_references[0]
        culprit = [d for d in s.deploys if d.commit == sha]
        assert len(culprit) == 1 and culprit[0].timestamp < first_error, entry["scenario"]


# ============================================================ 答案是找得到的
def test_every_key_reference_can_actually_be_found_in_the_data(scenarios):
    """好报告应当引用的关键出处，必须真的存在于工具能返回的数据里：否则"引用正确出处"这一项永远拿不到分。"""
    for name, s in scenarios.items():
        assert s.ground_truth.culprit_references, name
        commits = {d.commit for d in s.deploys}
        messages = " ".join(l.message for l in s.logs)
        for ref in s.ground_truth.culprit_references:
            assert ref in commits or ref in messages, (name, ref)


# ============================================================ 题目不泄露答案
def test_the_ticket_alone_never_passes_the_scoring_rule(scenarios):
    """只是把工单复述一遍的"答案"必须判错。"""
    for name, s in scenarios.items():
        ticket_text = f"{s.ticket.title} {s.ticket.description}"
        assert not text_is_correct(ticket_text, s.ground_truth.keywords), name


def test_ground_truth_and_root_cause_text_never_appear_in_tool_output(scenarios):
    for name, s in scenarios.items():
        visible = json.dumps(s.model_dump(mode="json", exclude={"ground_truth"}))  # 工具能读到的全部内容
        assert s.ground_truth.root_cause not in visible, name
        # runbook 只能描述"可能的原因"，不能直接写出这次的元凶（commit）
        runbook_text = " ".join(r.content for r in s.runbooks)
        for ref in s.ground_truth.culprit_references:
            if len(ref) == 7:  # commit 短哈希
                assert ref not in runbook_text, name


def test_runbooks_list_several_possible_causes_not_just_the_true_one(scenarios):
    for name, s in scenarios.items():
        relevant = next(r for r in s.runbooks if r.id.startswith("RB-3"))
        assert relevant.content.count(",") >= 3, name  # 列出了好几种可能，要靠日志才能判断是哪一种


# ============================================================ 评分器本身
def test_the_true_root_cause_text_is_always_scored_correct(scenarios):
    for name, s in scenarios.items():
        assert text_is_correct(s.ground_truth.root_cause, s.ground_truth.keywords), name


def test_the_scoring_rule_tells_all_the_categories_apart(scenarios):
    """别的类别的标准答案，在这个场景里必须判错：否则关键词分不清类别，准确率会虚高。"""
    by_category = {}
    for s in scenarios.values():
        by_category.setdefault(s.ground_truth.category, s)
    for category, s in by_category.items():
        for other_category, other in by_category.items():
            if other_category != category:
                assert not text_is_correct(other.ground_truth.root_cause, s.ground_truth.keywords, s.ground_truth.must_have), (category, other_category)


def test_scoring_unit_behaviour():
    keywords = ["certificate|cert ", "expired|expiry", "tls|ssl"]
    assert required_matches(keywords) == 2 and required_matches(["a"]) == 1
    assert matched_groups("The TLS CERTIFICATE has expired", keywords) == 3
    assert text_is_correct("certificate expired", keywords)
    assert not text_is_correct("the certificate looks fine", keywords)  # 只命中一个概念
    assert not text_is_correct("database is slow", keywords)
    assert text_is_correct("only concept", ["only concept"])  # 只有一个概念时，全中即可
    assert not text_is_correct("", keywords)


def test_difficulty_is_real_for_the_latest_deploy_heuristic(scenarios):
    """困难场景里，最近的一次发布是无辜的干扰项；这样才能区分"会推理"和"怪最近一次发布"。"""
    for entry in MANIFEST:
        s = scenarios[entry["scenario"]]
        own = sorted((d for d in s.deploys if d.service == s.ticket.service), key=lambda d: d.timestamp)
        latest = own[-1]
        is_culprit = latest.commit in s.ground_truth.culprit_references
        if entry["difficulty"] == "hard":
            assert not is_culprit and latest.version == "99.0.0-docs", entry["scenario"]
        elif entry["has_culprit_deploy"]:
            assert is_culprit, entry["scenario"]


# ============================================================ 评分器的"防投机"检查（比基线更严格）
def test_no_single_runbook_and_no_combination_of_runbooks_passes_the_scoring_rule(scenarios):
    for name, s in scenarios.items():
        gt = s.ground_truth
        texts = [f"{r.title} {r.content}" for r in s.runbooks] + [" ".join(f"{r.title} {r.content}" for r in s.runbooks)]
        for text in texts:
            assert not text_is_correct(text, gt.keywords, gt.must_have), name


def test_incident_specific_facts_never_appear_in_the_ticket_or_in_any_runbook(scenarios):
    """must_have 的每个写法都必须是"只有读过这次事故的日志和发布记录才知道"的事实。"""
    for name, s in scenarios.items():
        visible = " ".join([s.ticket.title, s.ticket.description, *(f"{r.title} {r.content}" for r in s.runbooks)]).lower()
        for group in s.ground_truth.must_have:
            for alternative in group.split("|"):
                assert alternative.strip().lower() not in visible, (name, alternative)


# ============================================================ 真实模型的回答：评分器审计的结论
# 这些是 Gemini 在第一次完整评估里的真实回答（逐字）。我把每个答错的案例都人工看过一遍：
#   下面"应判对"的，模型的诊断实质上是对的，只是评分规则的措辞太死（评分器的假阴性）；
#   下面"应判错"的，模型只说到了表面的错误（引擎处理不了日元），没有找到真正的根因（有人把功能开关打开了）。
# 把这些判断固定成测试，以后改评分规则时不会悄悄把结论改反。
GEMINI_ANSWERS_THAT_ARE_RIGHT = {
    "bug-2008": "The storefront-api experienced a traffic surge or increased request volume that saturated its worker queue (reaching size=2000), leading to RejectedExecutionException errors and HTTP 503 responses, despite no recent code deployments.",
    "bug-2018": "The search-api service experienced a traffic surge leading to public tenant rate limiting and worker queue saturation (queue full at size=2000, causing RejectedExecutionException and 503 errors), without any code changes being deployed.",
    "bug-2028": "The coupon-service experienced request saturation causing its worker queue to fill up (size=2000) and reject requests with 503 RejectedExecutionException, likely due to a traffic surge or increased load since no application code changes were deployed around the time.",
    "bug-2024": "shipping-api is timing out because its downstream dependency rates-service has its thread pool exhausted and cannot respond in time.",
}
GEMINI_ANSWERS_THAT_MISS_THE_ROOT_CAUSE = {
    "bug-2010": "The pricing-api service fails for international customers because the PricingEngineV2 does not support the JPY currency used in their requests.",
    "bug-2020": "The checkout-api is failing for customers in specific regions (such as CA-QC) due to missing tax region configuration data, rather than a code deployment.",
    "bug-2030": "The search-api service encounters SemanticRanker failures when processing non-latin queries from customers in certain countries because it produces empty embeddings.",
}


@pytest.mark.parametrize("name,answer", GEMINI_ANSWERS_THAT_ARE_RIGHT.items())
def test_substantively_correct_real_answers_are_scored_correct(scenarios, name, answer):
    gt = scenarios[name].ground_truth
    assert text_is_correct(answer, gt.keywords, gt.must_have)


@pytest.mark.parametrize("name,answer", GEMINI_ANSWERS_THAT_MISS_THE_ROOT_CAUSE.items())
def test_answers_that_stop_at_the_proximate_cause_stay_wrong(scenarios, name, answer):
    gt = scenarios[name].ground_truth
    assert not text_is_correct(answer, gt.keywords, gt.must_have)
