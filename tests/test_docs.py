"""文档的"防过时"检查：文档里引用的每个文件和目录必须真的存在，并且 Mermaid 图的代码块是成对闭合的。

文档写得再好，路径一过时就会误导读者（尤其是面试官顺着路径去找代码的时候）。重构、改名之后，这里会立刻报出来。
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", ROOT / "frontend" / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
ROOTS = ("app/", "scripts/", "tests/", "data/", "docs/", "frontend/", ".github/")
CODE_SPAN = re.compile(r"`([^`\n]+)`")


def referenced_paths(text: str) -> set[str]:
    found = set()
    for span in CODE_SPAN.findall(text):
        token = span.strip().split()[0] if span.strip() else ""
        token = token.rstrip(".,:;)")
        if not token.startswith(ROOTS):
            continue
        if any(marker in token for marker in ("*", "<", ">", "..", "{", "}", "$", "=")):
            continue  # 通配符、占位符、范围（bug-2001..2030）、命令行参数不是具体路径
        found.add(token)
    return found


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_path_a_document_mentions_exists(doc):
    missing = sorted(p for p in referenced_paths(doc.read_text(encoding="utf-8")) if not (ROOT / p.rstrip("/")).exists())
    assert missing == [], f"{doc.name} mentions files that do not exist: {missing}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_code_fences_are_balanced(doc):
    fences = [line for line in doc.read_text(encoding="utf-8").splitlines() if line.strip().startswith("```")]
    assert len(fences) % 2 == 0, f"{doc.name} has an unclosed code block"


def test_the_architecture_document_contains_both_diagrams():
    text = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert text.count("```mermaid") == 2 and "flowchart" in text and "sequenceDiagram" in text


def test_the_readme_links_to_every_document():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for doc in (ROOT / "docs").glob("*.md"):
        assert f"docs/{doc.name}" in readme, f"README does not link to docs/{doc.name}"
