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

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.llm.base import ToolSpec
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
    handler: Callable[[Any], Any]  # 接收校验后的参数对象，返回可被 JSON 序列化的结果


class ToolRegistry:
    """工具注册表：Agent 能用什么工具，全由这里说了算。"""

    def __init__(self, tools: list[Tool]):
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("duplicate tool names")

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

    def execute(self, name: str, raw_args: dict) -> Any:
        """执行模型请求的工具调用。raw_args 是模型给的、未经信任的参数。"""
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
        return tool.handler(args)


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


def build_investigation_tools(scenario: Scenario) -> ToolRegistry:
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

    return ToolRegistry(
        [
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
            Tool(
                name="search_runbooks",
                description="Search internal runbooks for known symptoms and recommended checks or mitigations.",
                args_model=SearchRunbooksArgs,
                handler=search_runbooks,
            ),
        ]
    )
