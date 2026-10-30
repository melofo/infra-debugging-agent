"""证据落地检查：报告里每条证据的出处，必须能在【工具真实返回的数据】里原样找到。

【为什么需要它】
模型会编造出处。比如它没查过日志，却在证据里写 "reference: checkout-api ERROR 14:02"，看起来非常可信。
工程师拿到报告，没法分辨哪条是真的查到的、哪条是模型"想象"出来的。
提示词里写"不许编造"不可靠（前面反复遇到过）；所以改成【代码强制】：
报告提交时，我们逐条核对证据，找不到原文的就拒绝，并把原因告诉模型让它改。
（和 Hypothesis 必须带证据是同一类规则：安全/质量规则由后端强制，不靠模型自觉。）

【怎么核对】三条规则，缺一不可：
  1. 出处必须是某个工具返回的字符串里的一段原文（忽略大小写和多余空白，其他一字不差）；
  2. 证据类型决定它能来自哪些工具：jira 证据只能来自 get_ticket，日志证据只能来自 search_logs……
     否则模型可以把工单里的字拿去冒充"日志证据"；
  3. 出处不能太短：reference 写成 "ERROR" 这种，在任何输出里都能找到，等于没核对。
  metric / trace 目前没有任何工具能产出，所以这两类证据一律通不过：宁可拒绝，也不放行无法核对的东西。

【它能防什么、不能防什么】
  能防：编造的 commit、编造的日志原文、用不相干的工具结果冒充出处、引用根本没查过的文档。
  不能防：出处是真的，但【解读】是错的（比如引用了真实日志，却说它证明了别的事）。
  这类问题要靠评估集来衡量（Phase 7），代码没法判断语义。所以"通过检查"只代表"出处可追溯"，不代表"结论正确"。
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from app.agent.common import SUBMIT_TOOL_NAME, SubmitRCAArgs
from app.llm.base import Message
from app.schemas import EvidenceSource

# 每类证据允许来自哪些工具。没有出现在这里的类型（metric、trace）没有任何工具能产出，永远无法核对。
SOURCE_TOOLS: dict[EvidenceSource, frozenset[str]] = {
    EvidenceSource.JIRA: frozenset({"get_ticket"}),
    EvidenceSource.LOG: frozenset({"search_logs"}),
    EvidenceSource.GIT: frozenset({"get_recent_deploys"}),
    EvidenceSource.RUNBOOK: frozenset({"search_runbooks", "search_knowledge"}),
    EvidenceSource.INCIDENT: frozenset({"search_knowledge"}),
}

# 出处至少多长。日志要求一段有意义的原文；工单号、runbook 编号、commit 短哈希本身就短。
MIN_REFERENCE_CHARS: dict[EvidenceSource, int] = {EvidenceSource.LOG: 20}
DEFAULT_MIN_REFERENCE_CHARS = 5

MAX_SHOWN_REFERENCE_CHARS = 120  # 反馈给模型时截断，避免把一大段文字原样回传


@dataclass(frozen=True)
class Ungrounded:
    hypothesis: int  # 第几个假设（从 1 开始）
    evidence: int  # 第几条证据（从 1 开始）
    source: str
    reference: str
    reason: str


def _normalize(text: str) -> str:
    # 只放宽"格式"：大小写、空白。其他任何字符都必须一致。
    return " ".join(text.split()).casefold()


def _leaves(value: Any) -> Iterator[str]:
    """把工具返回的数据（字典、列表、字符串……）拆成一个个字符串叶子。

    为什么不直接把整份输出转成一个大字符串再查：那样出处可以跨越两个不相干的字段拼出来
    （比如前一个字段的结尾 + 后一个字段的开头），也会匹配到 JSON 的引号和转义。逐个叶子比对更严格。
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _leaves(item)


class GroundingCorpus:
    """本次调查中，所有【成功的】工具调用返回过的数据，按工具名分组。"""

    def __init__(self) -> None:
        self._by_tool: dict[str, list[str]] = {}

    def add(self, tool_name: str, output: Any) -> None:
        if tool_name == SUBMIT_TOOL_NAME:
            return  # 模型自己提交的报告不能当作"证据来源"
        self._by_tool.setdefault(tool_name, []).extend(_normalize(leaf) for leaf in _leaves(output))

    @classmethod
    def from_messages(cls, messages: list[Message], extra: tuple = ()) -> "GroundingCorpus":
        """从对话历史里收集。只取工具消息里成功的结果：
        用户说的话、模型自己说的话、失败的工具报错，都不能当作证据来源。
        extra：同一批次里已经执行完、但还没追加进历史的工具结果。
        """
        corpus = cls()
        for message in messages:
            if message.role == "tool":
                for result in message.tool_results:
                    if not result.is_error:
                        corpus.add(result.name, result.result)
        for result in extra:
            if not result.is_error:
                corpus.add(result.name, result.result)
        return corpus

    def check(self, args: SubmitRCAArgs) -> list[Ungrounded]:
        problems: list[Ungrounded] = []
        for h_index, hypothesis in enumerate(args.hypotheses, start=1):
            for e_index, evidence in enumerate(hypothesis.evidence, start=1):
                reason = self._why_ungrounded(evidence.source, evidence.reference)
                if reason:
                    problems.append(Ungrounded(h_index, e_index, evidence.source.value, evidence.reference, reason))
        return problems

    def _why_ungrounded(self, source: EvidenceSource, reference: str) -> str | None:
        allowed = SOURCE_TOOLS.get(source)
        if allowed is None:
            return f"no tool can provide {source.value} evidence, so it cannot be verified; remove it"
        needle = _normalize(reference)
        minimum = MIN_REFERENCE_CHARS.get(source, DEFAULT_MIN_REFERENCE_CHARS)
        if len(needle) < minimum:
            return f"reference is too short to verify (at least {minimum} characters of the original text)"
        called = [tool for tool in sorted(allowed) if tool in self._by_tool]
        if not called:
            return f"{source.value} evidence must come from {' or '.join(sorted(allowed))}, which was not called successfully"
        if any(needle in leaf for tool in called for leaf in self._by_tool[tool]):
            return None
        return f"not found in the output of {' or '.join(called)}; copy the text exactly as the tool returned it"


def rejection_message(problems: list[Ungrounded]) -> str:
    """写给模型看的拒绝原因：具体到哪个假设的哪条证据，让它能改对。"""
    lines = []
    for p in problems:
        shown = p.reference if len(p.reference) <= MAX_SHOWN_REFERENCE_CHARS else p.reference[:MAX_SHOWN_REFERENCE_CHARS] + "..."
        lines.append(f"hypothesis {p.hypothesis}, evidence {p.evidence} ({p.source}, reference {shown!r}): {p.reason}")
    return (
        "report rejected: some evidence could not be verified against tool results. "
        "Each reference must be copied verbatim from data a tool returned in this conversation. "
        "Fix the references or call the missing tools, or remove the evidence "
        "(a hypothesis needs at least one verifiable piece of evidence). Problems: " + " | ".join(lines)
    )
