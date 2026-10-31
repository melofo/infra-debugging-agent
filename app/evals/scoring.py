"""给 agent 的报告打分：它找到的根因，对不对？

【为什么用关键词概念组，而不是让另一个大模型来评判】
大模型当裁判（LLM-as-judge）灵活，但贵、慢、不可重复，而且裁判自己也会错、会偏袒措辞漂亮的答案。
关键词规则是确定的、免费的、可以单元测试，缺点是粗糙：措辞特别的正确答案可能被判错，
措辞巧合的错误答案可能被判对。我们用三个办法控制这个风险：
  1. 关键词是"概念组"（同义写法用 | 连接），不是死板的单个单词；
  2. 至少命中 2 个概念才算对，不会因为碰巧提到一个常见词就蒙对；
  3. 还要出现"本次事故特有的事实"（must_have）：把 runbook 整段抄一遍能命中概念，但抄不出特有的事实；
  4. 用"标准答案必判对、别的类别的答案必判错、只复述工单必判错、整段抄 runbook 必判错"这几类测试检验评分器本身；
     再用不用大模型的基线（app/evals/baselines.py）在整个数据集上量一遍。
之后可以再叠加大模型裁判，用来复核有争议的案例，而不是替代这套规则。
"""

import math

from app.schemas import Hypothesis, RCAReport


def _groups(keywords: list[str]) -> list[list[str]]:
    return [[alt.strip().lower() for alt in group.split("|") if alt.strip()] for group in keywords]


def required_matches(keywords: list[str]) -> int:
    return min(2, len(keywords))


def matched_groups(text: str, keywords: list[str]) -> int:
    lowered = text.lower()
    return sum(1 for group in _groups(keywords) if any(alt in lowered for alt in group))


def text_is_correct(text: str, keywords: list[str], must_have: list[str] | tuple = ()) -> bool:
    """判对的条件：本次事故特有的事实【全部】出现，并且至少命中 2 个概念（见 GroundTruth.must_have 的说明）。"""
    lowered = text.lower()
    specifics = all(any(alt in lowered for alt in group) for group in _groups(list(must_have)))
    return specifics and matched_groups(text, keywords) >= required_matches(keywords)


def hypothesis_text(hypothesis: Hypothesis) -> str:
    """评分只看假设本身的论断（根因 + 建议的下一步），不看证据：证据是另一件事（出处是否可追溯），单独统计。"""
    return " ".join([hypothesis.root_cause, *hypothesis.suggested_next_steps])


def correct_rank(report: RCAReport, keywords: list[str], must_have: list[str] | tuple = ()) -> int | None:
    """第一个判对的假设排第几（从 1 开始）；没有判对的返回 None。假设已按置信度从高到低排好序。"""
    for rank, hypothesis in enumerate(report.hypotheses, start=1):
        if text_is_correct(hypothesis_text(hypothesis), keywords, must_have):
            return rank
    return None


def cites_culprit(report: RCAReport, references: list[str]) -> bool:
    """报告有没有引用到"关键出处"（元凶 commit 或最能说明问题的日志原文）。"""
    cited = " ".join(e.reference.lower() for h in report.hypotheses for e in h.evidence)
    return any(ref.lower() in cited for ref in references)


def calibration_gap(confidences_and_hits: list[tuple[float, bool]]) -> float | None:
    """平均置信度 - 实际准确率。正数 = 过度自信；负数 = 过度谦虚。样本为空返回 None。"""
    if not confidences_and_hits:
        return None
    mean_confidence = sum(c for c, _ in confidences_and_hits) / len(confidences_and_hits)
    accuracy = sum(1 for _, hit in confidences_and_hits if hit) / len(confidences_and_hits)
    return round(mean_confidence - accuracy, 4)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)]
