"""模型评分（LLM 裁判）：让另一次模型调用判断"这份报告的根因对不对"，作为确定性评分器的【第二意见】。

【为什么不作为主评分器】慢、花钱、不可复现，而且模型裁判偏爱写得流畅的回答；关键词评分器粗糙但可解释，还被测试守着。
所以它的位置是"第二意见"：两者一致就更放心，不一致的案例拿出来人工看（这正是上次发现评分器漏判的方法）。

【两条规矩】
  1. 裁判看得到标准答案，所以只能在评估里用，绝不能出现在 agent 的路径上（agent 的工具碰不到 ground_truth）。
  2. 裁判的回答只认第一个词 CORRECT / INCORRECT；其他任何回答都记为"无法判定"(None)，
     绝不默认当作"对"，也不悄悄丢掉：无法判定的数量要单独报告。
"""

from dataclasses import dataclass

from app.llm.base import LLMClient, LLMError
from app.schemas import RCAReport

JUDGE_SYSTEM = (
    "You grade an incident root-cause analysis. You are given the true root cause and the root cause an engineer "
    "proposed. Decide whether the proposal identifies the same underlying cause (wording may differ; naming only a "
    "symptom of it does not count). Answer with one word, CORRECT or INCORRECT, then a short reason."
)


@dataclass(frozen=True)
class Verdict:
    correct: bool | None  # None = 无法判定（裁判出错或回答不合格式）
    reason: str = ""


async def judge_report(llm: LLMClient, report: RCAReport, true_root_cause: str) -> Verdict:
    """只评最有把握的那个假设（和 Top-1 的口径一致）。"""
    top = report.hypotheses[0]
    prompt = f"<true_root_cause>{true_root_cause}</true_root_cause>\n<proposed_root_cause>{top.root_cause}</proposed_root_cause>"
    try:
        response = await llm.generate(prompt, system=JUDGE_SYSTEM)
    except LLMError as exc:
        return Verdict(None, f"judge failed: {exc}"[:200])
    text = response.text.strip()
    first = text.split(None, 1)[0].strip(".,:;*`\"'").upper() if text else ""
    if first not in ("CORRECT", "INCORRECT"):
        return Verdict(None, "unparseable judge answer")
    return Verdict(first == "CORRECT", text[:200])


def agreement(pairs: list[tuple[bool, Verdict]]) -> dict[str, int]:
    """把（确定性评分器的结论, 裁判的结论）两两对照。disagree 的案例是最值得人工看的。"""
    counts = {"both_correct": 0, "both_wrong": 0, "judge_only_correct": 0, "scorer_only_correct": 0, "undecided": 0}
    for scorer_correct, verdict in pairs:
        if verdict.correct is None:
            counts["undecided"] += 1
        elif scorer_correct and verdict.correct:
            counts["both_correct"] += 1
        elif not scorer_correct and not verdict.correct:
            counts["both_wrong"] += 1
        elif verdict.correct:
            counts["judge_only_correct"] += 1
        else:
            counts["scorer_only_correct"] += 1
    return counts
