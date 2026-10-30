"""知识库文档加载、校验和切块的测试（不需要数据库）。"""

import pytest

from app.rag.chunking import chunk_document
from app.rag.documents import Document, load_documents, parse_document

VALID = """---
id: RB-900
type: runbook
title: Example runbook
services: [checkout-api]
tags: [demo, 500]
---
# Example runbook

Intro text.

## Symptoms

Something is wrong.

## Mitigation

Fix it.
"""


# ------------------------------------------------------------ 加载与校验
def test_real_corpus_loads_with_unique_ids_and_valid_metadata():
    documents = load_documents()
    ids = [d.doc_id for d in documents]
    assert len(ids) == len(set(ids)) == 13
    assert {d.doc_type for d in documents} == {"runbook", "incident"}
    # 语料里应当包含 BUG-1042 场景要用到的文档，以及用来混淆的相似文档。
    assert {"RB-101", "RB-210", "INC-2031", "INC-2044"} <= set(ids)


def test_front_matter_is_parsed_into_a_document():
    doc = parse_document(VALID, "x.md")
    assert (doc.doc_id, doc.doc_type, doc.title) == ("RB-900", "runbook", "Example runbook")
    assert doc.services == ("checkout-api",)
    assert doc.body.startswith("# Example runbook")


def test_numeric_tags_are_kept_as_text():
    # YAML 会把 500 解析成整数；对标签来说它应当是字符串 "500"。
    assert parse_document(VALID, "x.md").tags == ("demo", "500")


def test_content_hash_changes_when_the_content_changes():
    a = parse_document(VALID, "x.md")
    b = parse_document(VALID.replace("Fix it.", "Fix it quickly."), "x.md")
    assert a.content_hash != b.content_hash
    assert a.content_hash == parse_document(VALID, "y.md").content_hash  # 同样的内容，哈希相同


@pytest.mark.parametrize(
    "text, message",
    [
        ("# no front matter at all", "missing front matter"),
        ("---\nid: RB-1\ntype: runbook\ntitle: T\n# never closed", "not closed"),
        ("---\nid: lowercase-1\ntype: runbook\ntitle: T\n---\nbody", "invalid front matter"),
        ("---\nid: RB-1\ntype: memo\ntitle: T\n---\nbody", "invalid front matter"),
        ("---\nid: RB-1\ntype: runbook\n---\nbody", "invalid front matter"),  # 缺少 title
        ("---\nid: RB-1\ntype: runbook\ntitle: T\n---\n   \n", "no content"),
        ("---\nid: [unclosed\n---\nbody", "invalid front matter"),  # YAML 本身语法错误
    ],
)
def test_malformed_documents_are_rejected_with_the_file_name(text, message):
    with pytest.raises(ValueError, match=message) as error:
        parse_document(text, "bad/file.md")
    assert "bad/file.md" in str(error.value)  # 报错时能定位到是哪个文件


def test_yaml_cannot_be_used_to_run_code(tmp_path):
    # 恶意文件试图让 YAML 加载器执行系统命令（在磁盘上创建一个标记文件）。
    # 不能只断言"文档被拒绝了"：不安全的加载器同样会拒绝它（命令执行后返回的 0 不是合法标题），
    # 但命令已经被执行了。所以要检查【副作用】：标记文件绝不能出现。
    marker = tmp_path / "pwned"
    evil = f"---\nid: RB-1\ntype: runbook\ntitle: !!python/object/apply:os.system ['touch {marker}']\n---\nbody"

    with pytest.raises(ValueError, match="invalid front matter"):
        parse_document(evil, "evil.md")

    assert not marker.exists()


def test_duplicate_ids_are_rejected(tmp_path):
    (tmp_path / "a.md").write_text(VALID)
    (tmp_path / "b.md").write_text(VALID)
    with pytest.raises(ValueError, match="duplicate document id RB-900"):
        load_documents(tmp_path)


def test_loading_order_is_deterministic(tmp_path):
    for n in (3, 1, 2):
        (tmp_path / f"d{n}.md").write_text(VALID.replace("RB-900", f"RB-90{n}"))
    assert [d.doc_id for d in load_documents(tmp_path)] == ["RB-901", "RB-902", "RB-903"]


# ------------------------------------------------------------ 切块
def doc(body: str, title: str = "T", doc_id: str = "RB-1") -> Document:
    return Document(doc_id, "runbook", title, (), (), body, "hash", "x.md")


def test_each_markdown_section_becomes_a_chunk_with_context():
    chunks = chunk_document(parse_document(VALID, "x.md"))

    assert [c.heading for c in chunks] == ["Example runbook", "Symptoms", "Mitigation"]
    assert [c.index for c in chunks] == [0, 1, 2]
    assert chunks[2].content == "Fix it."
    # 向量化的文本带着文档标题、编号和小节标题，让孤立的一句话也有上下文。
    assert chunks[2].embedding_text.startswith("Example runbook (RB-900) - Mitigation\n\nFix it.")
    assert "(RB-900)" in chunks[0].embedding_text and " - Example runbook" not in chunks[0].embedding_text


def test_empty_sections_produce_no_chunks():
    chunks = chunk_document(doc("## Empty\n\n## Real\n\ncontent"))
    assert [c.heading for c in chunks] == ["Real"]


def test_text_before_the_first_heading_is_kept():
    chunks = chunk_document(doc("preamble text\n\n## Section\n\nbody"))
    assert [c.heading for c in chunks] == [None, "Section"]


def test_a_document_without_headings_is_one_chunk():
    assert len(chunk_document(doc("just some plain text"))) == 1


def test_long_sections_are_split_and_every_piece_fits():
    paragraphs = [f"Paragraph {i}. " + "word " * 40 for i in range(12)]
    chunks = chunk_document(doc("## Long\n\n" + "\n\n".join(paragraphs)), max_chars=400, overlap_chars=60)

    assert len(chunks) > 3
    # 每块不超过上限（重叠部分额外允许一点）。
    assert all(len(c.content) <= 400 + 60 + 2 for c in chunks)
    # 内容没有丢失：每个段落的开头都能在某个块里找到。
    joined = "\n".join(c.content for c in chunks)
    assert all(f"Paragraph {i}." in joined for i in range(12))


def test_adjacent_chunks_overlap_so_boundary_sentences_keep_their_context():
    paragraphs = [f"Sentence number {i} is here. " * 6 for i in range(10)]
    chunks = chunk_document(doc("## Long\n\n" + "\n\n".join(paragraphs)), max_chars=300, overlap_chars=80)

    for previous, current in zip(chunks, chunks[1:]):
        tail_words = previous.content.split()[-3:]
        assert " ".join(tail_words) in current.content  # 下一块开头带着上一块的结尾


def test_a_single_huge_paragraph_is_split_on_word_boundaries():
    chunks = chunk_document(doc("## Big\n\n" + " ".join(["alpha"] * 500)), max_chars=200, overlap_chars=0)
    assert len(chunks) > 5
    assert all(set(c.content.split()) == {"alpha"} for c in chunks)  # 没有把单词切成两半


def test_chunking_is_deterministic():
    d = parse_document(VALID, "x.md")
    assert chunk_document(d) == chunk_document(d)


def test_real_corpus_chunks_are_all_non_empty_and_reasonably_sized():
    for document in load_documents():
        chunks = chunk_document(document)
        assert chunks, document.doc_id
        assert all(c.content.strip() and len(c.content) < 1300 for c in chunks), document.doc_id
        assert [c.index for c in chunks] == list(range(len(chunks)))
