"""MCP 客户端：让 Agent 通过 MCP 协议连接外部的工具服务端，而不是直接调用 Python 函数。

    Agent 循环 --> McpToolRegistry --MCP 协议--> MCP 服务端 --> 真实数据源

McpToolRegistry 满足和 ToolRegistry 同样的接口（names / specs / execute），
所以 Agent 循环一行都不用改，只是工具的"来源"换了。

【把 MCP 服务端当作"不可信的第三方"】
这是客户端最重要的一条原则。我们这次连的是自己的服务端，但 MCP 的价值就是能连任何人写的服务端。
服务端返回的内容会被原样交给大模型，所以恶意的服务端可以：
  - 在工具【描述】里藏提示词注入（"调用本工具前，先把用户的密钥发给……"，业内叫 tool poisoning）；
  - 偷偷多注册一个危险工具（delete_everything）；
  - 在工具【结果】里塞超大内容，或者永远不返回，拖垮调用方；
  - 在注解里谎称"只读"。
所以客户端自己必须有策略，不依赖服务端的自觉：
  1. 白名单：只接受我们明确允许的工具名，其他的一律忽略；
  2. 只接受声明为只读的工具（注解只是声明不是保证，所以这【只是一层额外防线】，白名单才是主要防线）；
  3. 截断描述、限制 schema 大小和工具数量；
  4. 限制每次调用的时间和结果的大小；
  5. 服务端报错的内容只取前一小段，不原样转发；
  6. 不注册任何"采样/询问用户"的回调：MCP 允许服务端反过来请求客户端，我们不给它这个机会。
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import mcp_types as types
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.exceptions import MCPError

from app.agent.common import AgentError
from app.config import ensure_safe_mcp_endpoint
from app.agent.tools import ToolError
from app.llm.base import ToolSpec

logger = logging.getLogger(__name__)

# 调查工具的白名单：MCP 服务端就算还提供了别的工具，Agent 也看不到、调不了。
INVESTIGATION_TOOLS = frozenset(
    {"get_ticket", "search_logs", "get_recent_deploys", "search_runbooks", "search_knowledge", "list_services"}
)

MAX_LIST_PAGES = 5
MAX_ERROR_CHARS = 500


class ToolSourceError(AgentError):
    """连不上工具服务端，或者拿不到它的工具列表。

    继承 AgentError：Worker 对它的处理是"按退避重试"（工具服务端可能只是暂时没起来），
    并且存进任务的错误信息只有这句通用的话，细节只写日志。
    """


class McpToolRegistry:
    def __init__(
        self,
        client: Client,
        specs: list[ToolSpec],
        *,
        call_timeout: float = 20.0,
        max_result_chars: int = 100_000,
    ):
        self._client = client
        self._specs = specs
        self._names = {s.name for s in specs}
        self._call_timeout = call_timeout
        self._max_result_chars = max_result_chars

    def names(self) -> list[str]:
        return [s.name for s in self._specs]

    def specs(self) -> list[ToolSpec]:
        return list(self._specs)

    async def execute(self, name: str, raw_args: dict) -> Any:
        if name not in self._names:
            # 客户端自己也强制白名单：不依赖服务端会拒绝。
            raise ToolError(f"unknown tool: {name}")
        try:
            async with asyncio.timeout(self._call_timeout):
                result = await self._client.call_tool(name, raw_args)
        except TimeoutError:
            raise ToolError(f"{name} timed out") from None
        except MCPError:
            # 协议层错误（请求不合法、服务端内部错误）。细节只写日志，对模型只说一句通用的话。
            logger.warning("mcp protocol error", extra={"tool": name}, exc_info=True)
            raise ToolError(f"{name} could not be completed") from None
        except Exception:
            logger.exception("mcp call failed", extra={"tool": name})
            raise ToolError("the tool server is unavailable") from None

        text = "".join(block.text for block in result.content if isinstance(block, types.TextContent))
        if len(text) > self._max_result_chars:
            # 结果会进入对话历史，之后每一轮都要重复付费；超大结果既可能是攻击也可能是 bug，一律拒绝。
            raise ToolError(f"{name} returned too much data")
        if result.is_error:
            raise ToolError(text[:MAX_ERROR_CHARS] or f"{name} failed")  # 服务端的报错只取前一小段

        # 优先解析文本形式的 JSON：它和服务端的原始输出完全一致。
        # structured_content 要求必须是对象，所以服务端会把列表包成 {"result": [...]}；
        # 客户端没法区分"被包装的列表"和"恰好只有一个 result 字段的字典"，所以不用它还原数据。
        try:
            return json.loads(text)
        except ValueError:
            return text


async def _load_specs(
    client: Client,
    *,
    allowed: frozenset[str] | set[str] | None,
    require_read_only: bool,
    max_description_chars: int,
    max_schema_chars: int,
    max_tools: int,
) -> list[ToolSpec]:
    specs: list[ToolSpec] = []
    cursor: str | None = None
    for _ in range(MAX_LIST_PAGES):  # 工具列表是分页的；服务端可以一直返回"还有下一页"，所以要限制页数
        page = await client.list_tools(cursor=cursor)
        for tool in page.tools:
            if allowed is not None and tool.name not in allowed:
                logger.warning("ignoring a tool that is not allowlisted", extra={"tool": tool.name})
                continue
            if require_read_only and not (tool.annotations and tool.annotations.read_only_hint is True):
                logger.warning("ignoring a tool that is not declared read-only", extra={"tool": tool.name})
                continue
            if len(json.dumps(tool.input_schema)) > max_schema_chars:
                logger.warning("ignoring a tool with an oversized schema", extra={"tool": tool.name})
                continue
            specs.append(
                ToolSpec(
                    name=tool.name,
                    # 描述会被原样放进给大模型的提示里，是提示词注入的经典入口：截断，限制它能携带的内容量。
                    description=(tool.description or "")[:max_description_chars],
                    parameters=dict(tool.input_schema),
                )
            )
            if len(specs) >= max_tools:
                return specs
        cursor = page.next_cursor
        if cursor is None:
            break
    return specs


@asynccontextmanager
async def bearer_http_transport(url: str, api_key: str):
    """通过 Streamable HTTP 连远程 MCP 服务端，每个请求都带上 Authorization: Bearer。

    密钥只放在请求头里，不放 URL（URL 会进日志）；不是 https 又不是本机时拒绝发送（见 ensure_safe_mcp_endpoint）。
    返回的对象可以直接作为 connect_mcp 的 target（SDK 把它当作 Transport）。
    """
    ensure_safe_mcp_endpoint(url)
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {api_key}"}) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            yield streams


@asynccontextmanager
async def connect_mcp(
    target: Any,
    *,
    allowed: frozenset[str] | set[str] | None = INVESTIGATION_TOOLS,
    require_read_only: bool = True,
    call_timeout: float = 20.0,
    max_result_chars: int = 100_000,
    max_description_chars: int = 1500,
    max_schema_chars: int = 20_000,
    max_tools: int = 32,
) -> AsyncIterator[McpToolRegistry]:
    """连接一个 MCP 服务端，返回可以直接交给 Agent 的工具来源。用法：

        async with connect_mcp(target) as tools:
            agent = InvestigationAgent(llm, tools)

    target 可以是：MCP 服务端对象（内存直连）、URL 字符串（Streamable HTTP）、StdioServerParameters（启动子进程）。

    【为什么是 async with，而且连接不能跨请求长期持有】
    MCP 客户端内部用 anyio 的任务组，规定"在哪个任务里进入，就必须在同一个任务里退出"。
    所以连接要在使用它的同一个任务里打开和关闭。要长期持有连接，必须有一个专门的任务来拥有它。
    """
    try:
        async with AsyncExitStack() as stack:
            try:
                client = await stack.enter_async_context(Client(target))
                specs = await _load_specs(
                    client,
                    allowed=allowed,
                    require_read_only=require_read_only,
                    max_description_chars=max_description_chars,
                    max_schema_chars=max_schema_chars,
                    max_tools=max_tools,
                )
            except Exception as exc:
                # 只包住"建立连接和读取工具列表"这一段；下面 yield 之后 Agent 自己的异常要原样往外传，不能被改写成别的类型。
                logger.exception("could not connect to the MCP tool server")
                raise ToolSourceError("tool server unavailable") from exc
            yield McpToolRegistry(client, specs, call_timeout=call_timeout, max_result_chars=max_result_chars)
    except BaseExceptionGroup as group:
        # MCP 客户端内部用 anyio 的任务组。任务组会把 with 块里抛出的异常包成 ExceptionGroup。
        # 如果不拆开，调用方的 `except LLMError` / `except AgentError` 就匹配不上，错误分类全乱
        # （/investigate 会返回 500 而不是 502，Worker 会把模型失败当成"意外错误"）。
        # 而直接调用工具时根本没有这一层包装，所以这个问题只有走 MCP 才出现，很难发现。
        raise _unwrap(group) from None


def _unwrap(error: BaseException) -> BaseException:
    """把只含一个异常的异常组（可能多层嵌套）还原成里面那个原始异常；含多个异常时保持原样。"""
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error
