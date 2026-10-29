"""手写循环（loop.py）和 SDK 版本（sdk_afc.py）共用的东西。

两个版本用同样的提示词、同样的工具、同样的输出格式，这样对比才公平：
唯一的区别就是"谁来跑循环"。
"""

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.llm.base import ToolSpec
from app.schemas import Hypothesis, RCAReport

# 系统提示词：规定调查方法和输出要求。注意最后一条规则只是"提醒"，
# 真正的强制靠 schemas.py 里的校验（Hypothesis 必须带证据），提示词不能当安全措施。
SYSTEM_PROMPT = """You are an infrastructure debugging agent. You investigate production incidents \
for engineers. You are strictly READ-ONLY: you can only query data with the provided tools.

Method:
1. Start by fetching the ticket.
2. Gather evidence: find when errors started (logs), what changed around that time (deploys), \
and what known guidance exists (runbooks).
3. Compare competing hypotheses. Do not stop at the first plausible one.
4. When you have enough evidence, call submit_rca_report exactly once to finish.

Rules:
- Every hypothesis must cite concrete evidence from tool results. Use the source and a reference \
(for example a commit sha or the log message) so a human can verify it.
- Confidence is between 0 and 1. If evidence is weak, lower it and say what to check next.
- Never invent data. If you lack information, say so in the summary."""

SUBMIT_TOOL_NAME = "submit_rca_report"


class SubmitRCAArgs(BaseModel):
    """submit_rca_report 这个"终止工具"的参数，也就是最终报告的内容。

    为什么用"工具调用"来交付最终结果，而不是让模型直接输出一段 JSON 文字？
    因为这样输出必须符合我们的 Pydantic 结构，校验不通过还能把错误回传给模型让它重写。
    这是 agent 里很常见的"结构化输出"做法。
    """

    summary: str = Field(min_length=1, description="2-3 sentence summary of the most likely root cause")
    hypotheses: list[Hypothesis] = Field(
        min_length=1, max_length=5, description="Root cause hypotheses; each needs at least one piece of evidence"
    )


def submit_spec() -> ToolSpec:
    return ToolSpec(
        name=SUBMIT_TOOL_NAME,
        description="Finish the investigation by submitting the root cause analysis report. Call it once, last.",
        parameters=SubmitRCAArgs.model_json_schema(),
    )


def build_report(ticket_id: str, args: SubmitRCAArgs) -> RCAReport:
    return RCAReport(incident_id=ticket_id, summary=args.summary, hypotheses=args.hypotheses)


class AgentError(Exception):
    """调查没能完成（超过最大步数、模型一直不提交报告等）。"""


@dataclass
class StepRecord:
    """调查过程中的一次工具调用记录。Chat UI 以后就是靠它展示"Agent 做了什么"。"""

    step: int
    tool: str
    args: dict
    ok: bool
    result_preview: str  # 结果的前几百个字符，完整结果可能很长


@dataclass
class InvestigationResult:
    report: RCAReport
    steps: list[StepRecord] = field(default_factory=list)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


def preview(value: object, limit: int = 300) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."
