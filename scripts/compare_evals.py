"""对比两次评估结果（改动前 vs 改动后），逐题配对，并给出精确符号检验的 p 值。

    .venv/bin/python scripts/compare_evals.py eval_results/run2.jsonl eval_results/v2-dev.jsonl

只比较两个文件里都有的场景。想比较"同一个场景集合"，先确认两次用的是同一个 --split。
"""

import argparse
import sys
from pathlib import Path

# 直接运行脚本时，Python 的模块搜索路径里是脚本所在的目录（scripts/）而不是项目根目录。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evals.compare import compare, format_comparison, reliability_warnings  # noqa: E402
from app.evals.runner import load_results, rescore, summarize  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare two evaluation result files.")
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument("--no-rescore", action="store_true", help="use the saved verdicts instead of re-scoring both with the current rule")
    args = parser.parse_args()

    a, b = load_results(Path(args.before)), load_results(Path(args.after))
    if not a or not b:
        print("one of the files has no results")
        return 2
    if not args.no_rescore:
        # 两边都用【当前的】评分规则重新打分：这样比较的差异只来自 agent，不会混进"评分规则在两次之间改过"。
        a, _ = rescore(a)
        b, _ = rescore(b)
    label_a = f"A({a[0].variant}@{a[0].max_steps})"
    label_b = f"B({b[0].variant}@{b[0].max_steps})"
    warnings = reliability_warnings(label_a, a) + reliability_warnings(label_b, b)
    for warning in warnings:
        print(warning)
    if warnings:
        print()
    print(format_comparison(compare(a, b), label_a, label_b, summarize(a)["by_category"], summarize(b)["by_category"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
