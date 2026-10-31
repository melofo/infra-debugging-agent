"""生成端到端评估用的 30 个故障场景，写进 data/scenarios/ 和 data/eval/agent_scenarios.json。

    .venv/bin/python scripts/build_eval_scenarios.py          # 重新生成（输出是确定的，和已提交的文件一致）
    .venv/bin/python scripts/build_eval_scenarios.py --check  # 只检查已提交的文件是不是和生成器一致（不写文件）

生成器在 app/evals/scenarios.py。场景是"生成物"：要改场景，改生成器再重新生成，不要手工改 JSON，
否则下次生成会把手工修改覆盖掉（测试里的一致性检查也会失败）。
"""

import argparse
import json
import sys
from pathlib import Path

# 直接运行脚本时，Python 的模块搜索路径里是脚本所在的目录（scripts/）而不是项目根目录。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evals.scenarios import build_all  # noqa: E402
from app.scenario import SCENARIO_DIR  # noqa: E402

MANIFEST_PATH = SCENARIO_DIR.parent / "eval" / "agent_scenarios.json"


def render(data: object) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the evaluation scenarios.")
    parser.add_argument("--check", action="store_true", help="only verify the committed files match the generator")
    args = parser.parse_args()

    scenarios, manifest = build_all()
    expected = {SCENARIO_DIR / f"{name}.json": render(data) for name, data in scenarios.items()}
    expected[MANIFEST_PATH] = render(manifest)

    if args.check:
        stale = [path.name for path, text in expected.items() if not path.is_file() or path.read_text(encoding="utf-8") != text]
        if stale:
            print(f"out of date: {', '.join(sorted(stale))}\nrun scripts/build_eval_scenarios.py")
            return 1
        print(f"{len(expected)} files match the generator")
        return 0

    for path, text in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(f"wrote {len(scenarios)} scenarios and the manifest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
