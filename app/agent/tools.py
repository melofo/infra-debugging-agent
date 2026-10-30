"""Agent 可以使用的工具，以及管理它们的注册表（ToolRegistry）。

回顾 agent loop：模型只会"提议"调用某个工具（给出工具名 + 参数），真正执行的是这里的代码。
所以这一层就是安全边界，我们在这里做三件事：
1. 白名单：只有注册过的工具才能被执行，模型编造一个工具名会被拒绝。
2. 参数校验：模型给的参数不可信（可能缺字段、类型错、超范围），执行前必须校验。
3. 限制范围：例如日志查询最多返回 50 条，防止一次把大量数据（和 token）塞给模型。

一个工具 = 三样东西：
  名字 + 描述（写给模型看）  |  参数模型（Pydantic 类）  |  处理函数（真正干活）
其中 Pydantic 参数模型一鱼两吃：既用来【校验】参数，又能自动生成【JSON Schema】发给模型。
"""

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import psycopg
from pydantic import BaseModel, Field, ValidationError

from app.llm.base import LLMError, ToolSpec
from app.rag.retrieval import KnowledgeBase, KnowledgeIndexMismatch
from app.scenario import Scenario

logger = logging.getLogger(__name__)


class ToolError(Exception):
    """工具执行失败（工具不存在、参数不合法、查不到数据等）。

    注意：agent loop 会把这个错误的文字【作为结果回传给模型】，而不是让整个调查崩溃。
    模型看到 "ticket not found" 后常常能自己改正，比如换一个参数重试。
    """


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]  # 参数的 Pydantic 类（注意存的是"类"本身，不是实例）
    # 接收校验后的参数对象，返回可被 JSON 序列化的结果。
    # 可以是普通函数（直接返回结果），也可以是 async 函数（返回协程，执行时会被 await），见 ToolRegistry.execute。
    handler: Callable[[Any], Any]


class ToolProvider(Protocol):
    """Agent 需要的"工具来源"的接口：说明书 + 执行。

    直接调用 Python 函数的 ToolRegistry、以及通过 MCP 协议连接外部服务端的 McpToolRegistry，
    都满足这个接口，所以 Agent 循环不需要知道工具到底是怎么执行的。（面向接口编程，和 Java 里一样。）
    """

    def names(self) -> list[str]: ...

    def specs(self) -> list[ToolSpec]: ...

    async def execute(self, name: str, raw_args: dict) -> Any: ...


class ToolRegistry:
    """工具注册表：Agent 能用什么工具，全由这里说了算。"""

    def __init__(self, tools: list[Tool]):
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("duplicate tool names")

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        """生成发给模型的工具说明书。"""
        return [
            ToolSpec(
                name=tool.name,
                description=tool.description,
                # model_json_schema()：Pydantic 根据类的字段和类型自动生成 JSON Schema，
                # 不用手写，也不会和校验规则不一致。
                parameters=tool.args_model.model_json_schema(),
            )
            for tool in self._tools.values()
        ]

    async def execute(self, name: str, raw_args: dict) -> Any:
        """执行模型请求的工具调用。raw_args 是模型给的、未经信任的参数。

        这是 async 方法，因为有些工具要访问数据库、调用外部接口（比如知识库检索），这些都是异步 I/O。
        同步的工具（读内存里的数据）照常工作：handler 返回的如果是普通值就直接用，
        如果返回的是"可等待对象"（调用 async 函数得到的协程），就 await 它。
        """
        tool = self._tools.get(name)
        if tool is None:
            # 白名单检查：不在注册表里就拒绝。
            raise ToolError(f"unknown tool: {name}")
        try:
            args = tool.args_model.model_validate(raw_args)
        except ValidationError as exc:
            # 把校验失败的原因告诉模型，方便它自己修正参数。
            raise ToolError(f"invalid arguments for {name}: {exc.errors(include_url=False)}") from exc
        logger.info("tool executed", extra={"tool": name})
        result = tool.handler(args)
        if inspect.isawaitable(result):
            result = await result
        return result


# ---------------------------------------------------------------------------
# 参数模型：描述每个工具接收什么参数。Field(description=...) 会进入 JSON Schema，
# 模型靠这些描述理解每个参数该怎么填，所以描述要写清楚。
# ---------------------------------------------------------------------------
class GetTicketArgs(BaseModel):
    ticket_id: str = Field(description="Jira ticket id, e.g. BUG-1042")


class SearchLogsArgs(BaseModel):
    service: str = Field(description="Service name whose logs to search, e.g. checkout-api")
    query: str = Field(default="", description="Case-insensitive text that must appear in the log message. Empty matches all.")
    level: str | None = Field(default=None, description="Only return this level: INFO, WARN or ERROR")
    # ge/le 限制取值范围：模型就算想一次拉 10000 条也会被拒绝。
    limit: int = Field(default=20, ge=1, le=50, description="Max entries to return (1-50)")


class GetRecentDeploysArgs(BaseModel):
    service: str = Field(description="Service name, e.g. checkout-api")
    limit: int = Field(default=5, ge=1, le=20, description="Max deploys to return, newest first")


class SearchRunbooksArgs(BaseModel):
    query: str = Field(description="Keywords describing the symptom, e.g. 'connection pool timeout'")


class SearchKnowledgeArgs(BaseModel):
    query: str = Field(
        min_length=3,
        max_length=500,
        description="Describe the symptom or question in natural language, e.g. 'requests time out waiting for a database connection'",
    )
    doc_type: Literal["runbook", "incident"] | None = Field(
        default=None, description="Only runbooks or only past incident write-ups. Omit to search both."
    )
    service: str | None = Field(
        default=None, max_length=64, description="Only documents for this service (plus generic ones). Omit to search all."
    )
    limit: int = Field(default=5, ge=1, le=8, description="Max results (1-8)")


def build_investigation_tools(scenario: Scenario, knowledge: KnowledgeBase | None = None) -> ToolRegistry:
    """为某个故障场景创建工具集。

    处理函数都定义在这个函数里面，用到了外层的 scenario 变量（闭包 closure，
    类似 Java 里的 lambda 捕获外部变量）。这样每个工具自然就"绑定"到这个场景的数据上。
    注意：这里只用了 ticket/logs/deploys/runbooks，从不碰 scenario.ground_truth。
    """

    # 场景里出现过的所有服务名。模型有时会把服务名拼错（例如 payment-service 写成 payments-service 的变体），
    # 如果工具对不存在的服务静默返回空结果，模型会误以为"这个服务没有数据"，得出错误结论。
    # 所以未知服务要明确报错，并告诉模型有哪些合法的名字，它就能自己改正。
    known_services = sorted(
        {scenario.ticket.service}
        | {entry.service for entry in scenario.logs}
        | {deploy.service for deploy in scenario.deploys}
    )

    def require_known_service(service: str) -> None:
        if service not in known_services:
            raise ToolError(f"unknown service {service!r}; known services: {', '.join(known_services)}")

    def get_ticket(args: GetTicketArgs) -> dict:
        if args.ticket_id != scenario.ticket.id:
            raise ToolError(f"ticket not found: {args.ticket_id}")
        # mode="json" 把 datetime 等类型转成 JSON 能表示的字符串。
        return scenario.ticket.model_dump(mode="json")

    def search_logs(args: SearchLogsArgs) -> dict:
        require_known_service(args.service)
        matches = [
            entry
            for entry in scenario.logs
            if entry.service == args.service
            and args.query.lower() in entry.message.lower()
            and (args.level is None or entry.level.upper() == args.level.upper())
        ]
        matches.sort(key=lambda entry: entry.timestamp)
        returned = matches[: args.limit]
        # 返回 total_matches，让模型知道结果有没有被截断；被截断时它可以缩小查询条件。
        return {
            "total_matches": len(matches),
            "returned": len(returned),
            "entries": [entry.model_dump(mode="json") for entry in returned],
        }

    def get_recent_deploys(args: GetRecentDeploysArgs) -> list[dict]:
        require_known_service(args.service)
        deploys = [d for d in scenario.deploys if d.service == args.service]
        deploys.sort(key=lambda d: d.timestamp, reverse=True)  # 最新的在前
        return [d.model_dump(mode="json") for d in deploys[: args.limit]]

    def search_runbooks(args: SearchRunbooksArgs) -> list[dict]:
        # 简单的关键词打分：查询里的每个词在标题+内容中出现就加一分。
        # Phase 5 会换成 pgvector 的语义检索（RAG），这里先用最简单的办法让流程跑通。
        words = args.query.lower().split()
        scored = []
        for runbook in scenario.runbooks:
            text = f"{runbook.title} {runbook.content}".lower()
            score = sum(1 for word in words if word in text)
            if score > 0:
                scored.append((score, runbook))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [runbook.model_dump(mode="json") for _, runbook in scored[:3]]

    async def search_knowledge(args: SearchKnowledgeArgs) -> dict:
        """语义检索 runbook 和历史事故。这是 async 的：要调向量化接口，还要查数据库。"""
        assert knowledge is not None
        try:
            if args.service is not None:
                known = await knowledge.known_services()
                if args.service not in known:
                    # 和日志、发布工具一样：写错服务名要明确报错并给出合法名字，不能静默返回空结果。
                    raise ToolError(f"unknown service {args.service!r}; known services: {', '.join(known)}")
            hits = await knowledge.search(
                args.query,
                k=args.limit,
                doc_types=[args.doc_type] if args.doc_type else None,
                services=[args.service] if args.service else None,
                max_per_doc=2,  # 每篇文档最多 2 块，让结果覆盖更多不同的文档，而不是被同一篇的各节占满
            )
        except ToolError:
            raise
        except KnowledgeIndexMismatch:
            # 内部配置问题。细节只写日志，模型和用户看到的是一句不含内部信息的话。
            logger.error("knowledge base was indexed with a different embedding model", exc_info=True)
            raise ToolError("knowledge search is unavailable until the knowledge base is re-indexed") from None
        except (LLMError, psycopg.Error):
            logger.warning("knowledge search failed", exc_info=True)
            raise ToolError("knowledge search is temporarily unavailable; continue with the other tools") from None

        if not hits:
            return {"results": [], "note": "no matching documents; try different wording or remove the filters"}
        return {
            "results": [
                {
                    "doc_id": hit.doc_id,  # 引用时用它，例如 RB-101、INC-2031
                    "doc_type": hit.doc_type,
                    "title": hit.title,
                    "section": hit.heading,
                    "relevance": round(hit.score, 3),  # 余弦相似度，越大越相关
                    "content": hit.content,
                }
                for hit in hits
            ]
        }

    tools = [
        Tool(
            name="get_ticket",
            description="Fetch a Jira ticket (title, description, service, severity, report time).",
            args_model=GetTicketArgs,
            handler=get_ticket,
        ),
        Tool(
            name="search_logs",
            description="Search application logs of one service. Use it to find errors and when they started.",
            args_model=SearchLogsArgs,
            handler=search_logs,
        ),
        Tool(
            name="get_recent_deploys",
            description="List recent deployments of a service with commit and diff summary. Use it to find changes near the incident time.",
            args_model=GetRecentDeploysArgs,
            handler=get_recent_deploys,
        ),
    ]
    if knowledge is not None:
        tools.append(
            Tool(
                name="search_knowledge",
                description=(
                    "Search internal runbooks and past incident write-ups by meaning. Describe the symptom in "
                    "natural language. Results are guidance from documentation and from other incidents, not "
                    "evidence about this one: confirm them against this incident's own logs and deploys."
                ),
                args_model=SearchKnowledgeArgs,
                handler=search_knowledge,
            )
        )
    else:
        # 知识库不可用时（没入库、pgvector 不可用、没配置向量化器）的兜底：只用关键词匹配场景自带的 runbook。
        tools.append(
            Tool(
                name="search_runbooks",
                description="Search internal runbooks for known symptoms and recommended checks or mitigations.",
                args_model=SearchRunbooksArgs,
                handler=search_runbooks,
            )
        )
    return ToolRegistry(tools)
