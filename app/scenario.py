"""模拟故障场景（scenario）：一个 JSON 文件 = 一次完整的"故障现场"。

为什么用 JSON 文件而不是数据库？
- 现在只需要一个可重复的、确定的"假世界"供 Agent 调查，文件最简单。
- Phase 7 做评估时，我们会写 30 到 50 个同样格式的场景，每个都有已知的正确根因，
  然后批量跑 Agent，统计它找对根因的比例。所以格式现在就要定好。

【重要设计】ground_truth（标准答案）和证据数据放在同一个文件里，但代码上严格分开：
工具（tools）只能读 ticket/logs/deploys/runbooks，永远碰不到 ground_truth，
只有评估代码才会读它。否则等于把答案发给了考生，评估就没有意义了。
"""

import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from app.schemas import Incident

# __file__ 是当前文件的路径；parents[1] 是它上两级目录，也就是项目根目录。
# 这样不管你从哪个目录启动程序，都能找到 data/，相当于 Java 里用 classpath 而不是相对路径。
SCENARIO_DIR = Path(__file__).resolve().parents[1] / "data" / "scenarios"
SCENARIO_NAME_PATTERN = re.compile(r"[a-z0-9-]+")


class LogEntry(BaseModel):
    timestamp: datetime
    service: str
    level: str
    message: str


class Deploy(BaseModel):
    timestamp: datetime
    service: str
    version: str
    commit: str
    author: str
    summary: str
    diff_summary: str  # 这次改动改了什么，真实系统里来自 git diff


class Runbook(BaseModel):
    id: str
    title: str
    content: str


class GroundTruth(BaseModel):
    """标准答案，只给评估用，绝不能暴露给 Agent 或它的工具。"""

    root_cause: str
    # 关键词按"概念组"写：每一项是一个概念，用 | 分隔同义写法（任意一个出现就算这个概念出现）。
    # 评分时，假设里命中至少 2 个概念才算判对（只有 1 个概念的话要全中）。见 app/evals/scoring.py。
    keywords: list[str]
    # 必须出现的"本次事故特有的事实"（版本号、配置项名、依赖名、功能开关名……），每一项也是 | 分隔的同义写法。
    # 为什么需要它：runbook 会列出好几种可能的原因，把 runbook 原文整段抄一遍，就能命中很多概念关键词，
    # 但那不是诊断。特有的事实只存在于这次事故的数据里，抄 runbook 抄不出来，必须真的读过日志和发布记录。
    must_have: list[str] = []
    # 以下是评估用的补充信息（默认值让旧的场景文件不用改）：
    category: str = ""  # 根因的类别，用来分组统计（哪一类故障 agent 最容易判错）
    # 一份好报告应该引用的"关键出处"：真正的元凶 commit，或者最能说明问题的那条日志里的一段原文。
    culprit_references: list[str] = []


class Scenario(BaseModel):
    ticket: Incident  # 复用 Phase 1 定义好的数据结构
    logs: list[LogEntry]
    deploys: list[Deploy]
    runbooks: list[Runbook]
    ground_truth: GroundTruth


def load_scenario(name: str) -> Scenario:
    # 安全：name 最终可能来自 HTTP 请求。如果不检查，有人传 "../../.env" 就能读到项目外的文件，
    # 这叫"路径穿越"（path traversal），是最经典的 Web 漏洞之一。
    # 白名单做法：只允许小写字母、数字和短横线，其他一律拒绝。
    if not SCENARIO_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"invalid scenario name: {name!r}")
    # model_validate_json：读入 JSON 文本并按上面的类型做校验，
    # 数据文件写错了（缺字段、时间格式不对）会在加载时立刻报错，而不是调查到一半才崩。
    path = SCENARIO_DIR / f"{name}.json"
    return Scenario.model_validate_json(path.read_text(encoding="utf-8"))


def scenario_exists(name: str) -> bool:
    """名字合法并且对应的场景文件存在。"""
    return bool(SCENARIO_NAME_PATTERN.fullmatch(name)) and (SCENARIO_DIR / f"{name}.json").is_file()
