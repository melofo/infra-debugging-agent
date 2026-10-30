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
and what known guidance exists (runbooks and past incidents, via the knowledge or runbook search tool).
3. Compare competing hypotheses. Do not stop at the first plausible one.
4. When you have enough evidence, call submit_rca_report. After it is accepted, reply to the \
engineer in one or two plain sentences that directly answer their message.

Follow-up questions:
After the report is submitted the engineer may ask follow-up questions. Answer in concise plain \
text. Reuse data you already gathered earlier in the conversation; call tools only if you need new \
data. Only call submit_rca_report again if the engineer asks you to revise the report.

Rules:
- Every hypothesis must cite concrete evidence from tool results. The reference must be exactly \
verifiable by a human: a commit sha, the exact log message, a runbook id or a ticket id. \
Never use a tool name as a reference.
- If the engineer asked a specific question, the report summary must answer it directly.
- Runbooks and past incidents are guidance, not proof. A similar past incident on another service tells \
you where to look; only this incident's own logs, deploys and ticket can support a conclusion. Cite them \
only as supporting context, with the document id (for example RB-101 or INC-2031) as the reference and \
the source runbook or incident.
- Confidence is between 0 and 1. If evidence is weak, lower it and say what to check next. \
Do not use 1.0: you cannot be completely certain from logs alone.
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
        description="Finish the investigation by submitting the root cause analysis report, once all evidence is gathered.",
        parameters=SubmitRCAArgs.model_json_schema(),
    )


def session_system_prompt(ticket_id: str) -> str:
    """会话专用的系统提示词：在通用提示词后面补上"这个会话在调查哪张工单"。

    这样用户的第一句话即使没写工单号（例如只说"开始调查"），模型也知道该查哪一个。
    """
    return f"{SYSTEM_PROMPT}\n\nThis conversation is about ticket {ticket_id}."


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
    """一轮对话（turn）的结果。

    首轮调查：report 有值。后续追问：通常只有 answer（文字），除非模型被要求修订报告。
    """

    answer: str
    report: RCAReport | None = None
    steps: list[StepRecord] = field(default_factory=list)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


def preview(value: object, limit: int = 300) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."
