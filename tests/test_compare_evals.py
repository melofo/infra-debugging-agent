import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.evals.agent_eval import CaseResult
from app.evals.compare import compare, format_comparison, reliability_warnings, sign_test_p_value


def r(scenario, top1, repeat=0, category="c"):
    return CaseResult(scenario=scenario, category=category, difficulty="easy", has_culprit_deploy=True, top1=top1, repeat=repeat)


def test_the_sign_test_matches_hand_computed_values():
    assert sign_test_p_value(0, 0) == 1.0  # 没有任何变化：没有证据
    assert sign_test_p_value(5, 0) == pytest.approx(2 / 32)  # 5 道全朝好的方向：0.0625，小样本不够显著
    assert sign_test_p_value(9, 1) == pytest.approx(2 * (1 + 10) / 1024)  # 0.0215
    assert sign_test_p_value(1, 9) == sign_test_p_value(9, 1)  # 双侧：方向不影响
    assert sign_test_p_value(5, 5) == 1.0  # 一半一半：纯属波动
    assert sign_test_p_value(3, 2) == pytest.approx(min(1.0, 2 * (1 + 5 + 10) / 32))
    assert 0 < sign_test_p_value(20, 3) < 0.001


def test_only_changed_cases_matter_and_ties_are_ignored():
    a = [r("s1", False), r("s2", False), r("s3", True), r("s4", True), r("s5", False)]
    b = [r("s1", True), r("s2", True), r("s3", True), r("s4", False), r("s5", False)]
    c = compare(a, b)
    assert (c.improved, c.regressed, c.unchanged) == (["s1", "s2"], ["s4"], 2)
    assert (c.top1_a, c.top1_b) == (0.4, 0.6)
    assert c.verdict == "not distinguishable from chance"  # 2 好 1 坏，远不够下结论


def test_a_large_consistent_change_is_called_better_and_a_reverse_one_worse():
    a = [r(f"s{i}", False) for i in range(10)]
    b = [r(f"s{i}", True) for i in range(10)]
    assert compare(a, b).verdict == "better" and compare(b, a).verdict == "worse"
    assert compare(a, a).verdict == "no difference on any case"


def test_repeats_are_averaged_per_scenario_before_comparing():
    a = [r("s1", True, 0), r("s1", False, 1)]  # 0.5
    b = [r("s1", True, 0), r("s1", True, 1)]  # 1.0
    c = compare(a, b)
    assert c.improved == ["s1"] and c.top1_a == 0.5 and c.top1_b == 1.0


def test_scenarios_missing_from_one_side_are_reported_not_silently_dropped():
    c = compare([r("s1", True), r("only_a", True)], [r("s1", False), r("only_b", True)])
    assert c.scenarios == 1 and c.only_in_one == ["only_a", "only_b"] and c.regressed == ["s1"]


def test_the_report_names_both_sides_and_the_changed_cases():
    c = compare([r("s1", False, category="x")], [r("s1", True, category="x")])
    text = format_comparison(c, "A", "B", {"x": {"top1": 0.0}}, {"x": {"top1": 1.0}})
    assert "newly correct (1): s1" in text and "exact sign test" in text and "A=  0.0%" in text and "B=100.0%" in text


def test_the_cli_compares_two_saved_files_after_rescoring_both(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "eval_agent.py"
    compare_script = Path(__file__).resolve().parents[1] / "scripts" / "compare_evals.py"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    for name, llm in (("good.jsonl", "oracle"), ("bad.jsonl", "ticket_echo")):
        done = subprocess.run([sys.executable, str(script), "--llm", llm, "--limit", "6", "--out", str(tmp_path / name)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
    result = subprocess.run([sys.executable, str(compare_script), str(tmp_path / "bad.jsonl"), str(tmp_path / "good.jsonl")], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "newly correct (6)" in result.stdout and "better" in result.stdout  # 6 道全部从错变对：p = 0.0312
    missing = subprocess.run([sys.executable, str(compare_script), str(tmp_path / "nope.jsonl"), str(tmp_path / "good.jsonl")], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert missing.returncode == 2


def test_a_run_that_is_mostly_errors_is_flagged_as_not_comparable():
    quota = [CaseResult(scenario=f"s{i}", category="c", difficulty="easy", has_culprit_deploy=True, status="llm_error", error="429 RESOURCE_EXHAUSTED quota") for i in range(9)]
    quota.append(r("s9", True))
    [warning] = reliability_warnings("B", quota)
    assert "9/10 errored runs" in warning and "429" in warning and "re-run" in warning
    assert reliability_warnings("A", [r(f"s{i}", i % 2 == 0) for i in range(10)]) == []  # 答错不是出错
    few_errors = [r(f"s{i}", True) for i in range(9)] + [CaseResult(scenario="s9", category="c", difficulty="easy", has_culprit_deploy=True, status="timeout")]
    assert reliability_warnings("A", few_errors) == []  # 10% 的出错属于正常波动
    assert reliability_warnings("A", []) == []


def test_the_cli_warns_when_a_file_is_mostly_errors(tmp_path):
    import json

    good = tmp_path / "good.jsonl"
    bad = tmp_path / "bad.jsonl"
    script = Path(__file__).resolve().parents[1] / "scripts" / "eval_agent.py"
    compare_script = Path(__file__).resolve().parents[1] / "scripts" / "compare_evals.py"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    subprocess.run([sys.executable, str(script), "--llm", "oracle", "--limit", "5", "--out", str(good)], cwd=tmp_path, env=env, check=True, capture_output=True)
    rows = [json.loads(line) for line in good.read_text().splitlines()]
    for row in rows:
        row.update(status="llm_error", error="429 RESOURCE_EXHAUSTED", top1=False, top3=False, report=None)
    bad.write_text("".join(json.dumps(row) + "\n" for row in rows))
    result = subprocess.run([sys.executable, str(compare_script), str(good), str(bad)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "WARNING" in result.stdout and "429" in result.stdout
