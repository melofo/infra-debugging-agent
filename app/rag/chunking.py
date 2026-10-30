"""把文档切成适合检索的"块"（chunk）。

【为什么要切块？】
1. 向量化模型有输入长度限制；
2. 更重要的是检索精度：一整篇文档只能得到一个向量，这个向量是整篇内容的"平均"，
   某一小段很相关的内容会被稀释。切成小块后，每块的向量只代表一段话，匹配更准；
3. 给模型的上下文也更省：只把相关的几段发给它，而不是整篇文档。

【怎么切】优先按 Markdown 标题切，一个标题下的内容就是一个自然的语义单元（症状、诊断、缓解……）。
某一节太长时再按段落切，并让相邻两块有一点【重叠】：
否则恰好被切断在边界上的句子，两边各缺一半上下文，哪块都检索不好。

【块的两个文本】
  content         原文，展示给人和模型看
  embedding_text  = "文档标题 (编号) — 小节标题" + 原文，用来做向量化。
  给每块加上文档标题和小节标题，是一个常见的检索优化：块本身可能只写"restore the previous value"，
  脱离上下文根本不知道在说什么；带上标题后，向量里就包含了"这是连接池 runbook 的缓解措施"。
"""

import re
from dataclasses import dataclass

from app.rag.documents import Document

DEFAULT_MAX_CHARS = 1000
DEFAULT_OVERLAP_CHARS = 120

_HEADING = re.compile(r"^(#{1,3})[ \t]+(.+?)[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class Chunk:
    index: int
    heading: str | None
    content: str
    embedding_text: str


def _sections(body: str) -> list[tuple[str | None, str]]:
    """按 Markdown 标题把正文切成 (标题, 内容) 的列表；第一个标题之前的内容标题为 None。"""
    matches = list(_HEADING.finditer(body))
    if not matches:
        return [(None, body.strip())] if body.strip() else []
    sections: list[tuple[str | None, str]] = []
    preamble = body[: matches[0].start()].strip()
    if preamble:
        sections.append((None, preamble))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        text = body[match.end() : end].strip()
        if text:  # 只有标题没有内容的节直接丢弃：空块没有任何检索价值
            sections.append((match.group(2), text))
    return sections


def _hard_split(paragraph: str, max_chars: int) -> list[str]:
    """一个段落本身就超长时，按单词边界硬切（不会把一个单词切成两半）。"""
    if len(paragraph) <= max_chars:
        return [paragraph]
    parts, current = [], ""
    for word in paragraph.split():
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= max_chars:
            current = candidate
        else:
            parts.append(current)
            current = word
    if current:
        parts.append(current)
    return parts


def _split_long(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    current = ""
    for paragraph in (p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()):
        for part in _hard_split(paragraph, max_chars):
            candidate = f"{current}\n\n{part}" if current else part
            if len(candidate) <= max_chars:
                current = candidate
            else:
                pieces.append(current)
                current = part
    if current:
        pieces.append(current)

    if overlap_chars <= 0 or len(pieces) == 1:
        return pieces
    # 重叠：每一块开头带上前一块的结尾。从单词边界开始取，避免半个单词。
    overlapped = [pieces[0]]
    for previous, piece in zip(pieces, pieces[1:]):
        tail = previous[-overlap_chars:]
        if " " in tail:
            tail = tail[tail.index(" ") + 1 :]
        overlapped.append(f"{tail}\n\n{piece}")
    return overlapped


def chunk_document(
    document: Document, max_chars: int = DEFAULT_MAX_CHARS, overlap_chars: int = DEFAULT_OVERLAP_CHARS
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for heading, text in _sections(document.body):
        for piece in _split_long(text, max_chars, overlap_chars):
            header = f"{document.title} ({document.doc_id})"
            if heading and heading != document.title:
                header += f" - {heading}"
            chunks.append(Chunk(len(chunks), heading, piece, f"{header}\n\n{piece}"))
    return chunks
