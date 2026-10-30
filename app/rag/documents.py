"""知识库文档的加载与校验。

文档是带 YAML "前置元数据"（front matter）的 Markdown 文件：

    ---
    id: RB-101
    type: runbook
    title: Database connection pool exhaustion
    services: [checkout-api]
    tags: [database]
    ---
    # 正文……

元数据（id、类型、适用服务）后面检索时要用来【过滤】，所以必须在入库前就校验好，
而不是等到检索出了怪结果才发现某篇文档的元数据写错了。
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "data" / "knowledge"


class FrontMatter(BaseModel):
    id: str = Field(pattern=r"^[A-Z]{2,4}-\d{1,6}$")  # 例如 RB-101、INC-2031
    type: Literal["runbook", "incident"]
    title: str = Field(min_length=1)
    services: list[str] = []  # 空列表表示"适用于所有服务"
    tags: list[str] = []

    @field_validator("tags", "services", mode="before")
    @classmethod
    def _numbers_are_text(cls, value):
        # YAML 的经典坑：`tags: [500]` 里的 500 会被解析成【整数】而不是字符串。
        # 标签 "500"（HTTP 状态码）完全合理，所以把数字转成字符串，而不是让作者去加引号。
        # 其他类型（布尔值、字典等）仍然交给后面的类型校验去拒绝。
        if isinstance(value, list):
            return [str(item) if isinstance(item, (int, float)) and not isinstance(item, bool) else item for item in value]
        return value


@dataclass(frozen=True)
class Document:
    doc_id: str
    doc_type: str
    title: str
    services: tuple[str, ...]
    tags: tuple[str, ...]
    body: str
    # 整个文件内容的 SHA-256。入库时用它判断"这篇文档变没变"：没变就不需要重新向量化（省钱省时间）。
    content_hash: str
    source: str  # 相对路径，出错时告诉人是哪个文件有问题


def parse_document(text: str, source: str) -> Document:
    if not text.startswith("---\n"):
        raise ValueError(f"{source}: missing front matter (file must start with '---')")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError(f"{source}: front matter is not closed with '---'")
    header, body = text[4:end], text[end + 5 :]
    try:
        # 一定要用 safe_load，不能用 yaml.load。
        # yaml.load 能解析 `!!python/object/apply:os.system [...]` 这样的标签，
        # 加载一个恶意文件就等于执行任意代码（和 Java 反序列化漏洞是同一类问题）。
        # safe_load 只允许字符串、数字、列表、字典这些基本类型。
        meta = yaml.safe_load(header)
        front = FrontMatter.model_validate(meta)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"{source}: invalid front matter: {exc}") from None
    body = body.strip()
    if not body:
        raise ValueError(f"{source}: document has no content")
    return Document(
        doc_id=front.id,
        doc_type=front.type,
        title=front.title,
        services=tuple(front.services),
        tags=tuple(front.tags),
        body=body,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        source=source,
    )


def load_documents(root: Path = KNOWLEDGE_DIR) -> list[Document]:
    documents: list[Document] = []
    seen: dict[str, str] = {}
    for path in sorted(root.rglob("*.md")):  # sorted：每次加载顺序一致，结果可重复
        source = str(path.relative_to(root))
        document = parse_document(path.read_text(encoding="utf-8"), source)
        if document.doc_id in seen:
            raise ValueError(f"duplicate document id {document.doc_id}: {seen[document.doc_id]} and {source}")
        seen[document.doc_id] = source
        documents.append(document)
    return documents
