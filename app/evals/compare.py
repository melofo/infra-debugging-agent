"""对比两次评估（改动前 vs 改动后）：同一道题，哪个版本答对了？

【为什么要"配对"比较，而不是比两个准确率】
30 题，73% 和 80% 只差 2 道题，而模型每次回答本来就有随机波动，光看两个百分数无法判断改动有没有用。
配对比较看的是"同一道题"：改动后【新答对】几道、【新答错】几道。两边打平的题（都对、都错）不提供任何信息，直接忽略。

【符号检验（exact sign test）】
如果改动其实没用，那么"新答对"和"新答错"应该各占一半（像抛硬币）。
在 n 道"结果变了的题"里，观察到比较少的那一边只有 k 道，这种事纯属巧合的概率就是二项分布的尾概率：
    p = 2 * P(X <= k),  X ~ Binomial(n, 0.5)        （双侧，最大取 1）
p 很小才有理由相信改动真的有效。它不依赖任何分布假设，样本很小也是精确的。
例：5 道题全部朝好的方向变，p = 2 * (1/2^5) = 0.0625，在 5% 的显著性水平下【还不够】——所以小样本很难"证明"什么。

重复运行（--repeat）的题，先对同一道题取 Top-1 的平均值，再比较。
"""

import math
from dataclasses import dataclass

from app.evals.agent_eval import CaseResult


def sign_test_p_value(better: int, worse: int) -> float:
    n = better + worse
    if n == 0:
        return 1.0
    k = min(better, worse)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


@dataclass
class Comparison:
    scenarios: int
    top1_a: float
    top1_b: float
    improved: list[str]
    regressed: list[str]
    unchanged: int
    p_value: float
    only_in_one: list[str]

    @property
    def verdict(self) -> str:
        if not self.improved and not self.regressed:
            return "no difference on any case"
        if self.p_value < 0.05:
            return "better" if len(self.improved) > len(self.regressed) else "worse"
        return "not distinguishable from chance"


def _per_scenario(results: list[CaseResult]) -> dict[str, float]:
    runs: dict[str, list[bool]] = {}
    for r in results:
        runs.setdefault(r.scenario, []).append(r.top1)
    return {name: sum(hits) / len(hits) for name, hits in runs.items()}


def compare(a: list[CaseResult], b: list[CaseResult]) -> Comparison:
    scores_a, scores_b = _per_scenario(a), _per_scenario(b)
    shared = sorted(set(scores_a) & set(scores_b))
    improved = [s for s in shared if scores_b[s] > scores_a[s]]
    regressed = [s for s in shared if scores_b[s] < scores_a[s]]
    return Comparison(
        scenarios=len(shared),
        top1_a=round(sum(scores_a[s] for s in shared) / len(shared), 4) if shared else 0.0,
        top1_b=round(sum(scores_b[s] for s in shared) / len(shared), 4) if shared else 0.0,
        improved=improved,
        regressed=regressed,
        unchanged=len(shared) - len(improved) - len(regressed),
        p_value=round(sign_test_p_value(len(improved), len(regressed)), 4),
        only_in_one=sorted(set(scores_a) ^ set(scores_b)),
    )


def format_comparison(c: Comparison, label_a: str, label_b: str, by_category_a: dict, by_category_b: dict) -> str:
    lines = [
        f"compared on {c.scenarios} scenarios present in both runs",
        f"Top-1   {label_a}: {c.top1_a * 100:.1f}%   {label_b}: {c.top1_b * 100:.1f}%",
        f"newly correct ({len(c.improved)}): {', '.join(c.improved) or '-'}",
        f"newly wrong   ({len(c.regressed)}): {', '.join(c.regressed) or '-'}",
        f"unchanged: {c.unchanged}   exact sign test p = {c.p_value}   -> {c.verdict}",
    ]
    if c.only_in_one:
        lines.append(f"not compared (only in one run): {', '.join(c.only_in_one)}")
    lines.append("-- by category (top-1)")
    for name in sorted(set(by_category_a) | set(by_category_b)):
        a = by_category_a.get(name, {}).get("top1")
        b = by_category_b.get(name, {}).get("top1")
        fmt = lambda v: "  n/a " if v is None else f"{v * 100:5.1f}%"  # noqa: E731
        lines.append(f"   {name:34} {label_a}={fmt(a)}  {label_b}={fmt(b)}")
    return "\n".join(lines)
