"""会话存储：一个会话 = 一次调查 + 之后所有的追问。

【状态放在服务端】会话的完整历史（含工具结果）只保存在服务端，浏览器只持有一个会话 id。
好处：请求小；工具结果可能很大或含敏感数据，不需要让客户端搬运；
更重要的是客户端无法篡改历史（否则有人可以伪造"工具返回了什么"来诱导模型）。

【两种存储，同一个接口（SessionStorage）】
  SessionStore          进程内存。重启就丢，多个实例之间也不共享。只用于本地开发和测试。
  PostgresSessionStore  数据库（默认）。重启不丢，多个实例共享同一份。

【多实例时要解决的两个并发问题，用的是和任务队列相同的思路】
  1. "同一时刻只处理一条消息"：不能再靠进程内的锁（别的实例看不到它）。改用数据库里的【租约】：
     原子地写入"我在处理，直到某个时间"，过期自动失效，所以持有者崩溃也不会把会话永久锁死。
  2. "别覆盖别人的写入"：保存时必须出示租约令牌，并且版本号没变（围栏 + 乐观锁）。
     租约过期被别人接手之后，原来的持有者即使晚到也写不进去。
"""

import asyncio
import json
import logging
import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import anyio
from fastapi import Request
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, TypeAdapter

from app.agent.common import StepRecord
from app.agent.common import AGENT_VARIANTS
from app.agent.tools import ToolRegistry, build_investigation_tools
from app.llm.base import Message
from app.llm.codec import message_from_json, message_to_json
from app.scenario import load_scenario
from app.schemas import RCAReport

logger = logging.getLogger(__name__)


class Turn(BaseModel):
    """给界面看的一轮对话记录（用户说了什么、Agent 答了什么、做了哪些步骤）。

    注意它和下面 Session.messages 的区别：
      messages = 发给模型的完整历史（内部格式，含原始工具结果，只在服务端用）
      turns    = 整理过的精简记录（对外展示，刷新页面时用它恢复界面）
    """

    user_message: str
    answer: str
    report: RCAReport | None = None
    steps: list[StepRecord]
    llm_calls: int
    input_tokens: int
    output_tokens: int
    latency_ms: int


@dataclass
class Session:
    id: str
    scenario_name: str
    ticket_id: str
    registry: ToolRegistry
    # 谁创建的这个会话（API key 的名字）。只有所有者（和管理员）能访问它：
    # 否则知道别人的会话 id 就能读到别人的整段对话，这就是 IDOR（不安全的直接对象引用）漏洞。
    owner: str = "anonymous-dev"
    uses_knowledge: bool = False  # 创建时有没有启用知识库检索（恢复会话时要按同样的方式重建工具）
    # 创建时用的 agent 配置（v1/v2）。和 uses_knowledge 同理：一个会话的工具集在整个生命周期里必须不变，
    # 否则历史里已经调用过 list_services 的会话，换回 v1 之后就找不到这个工具了。
    agent_variant: str = "v1"
    version: int = 0  # 数据库里的版本号（乐观锁）；内存存储用不到
    lease_token: str | None = None  # 持有租约时的令牌（数据库存储用）
    messages: list[Message] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    report: RCAReport | None = None  # 最新的报告；用来判断下一条消息是"首次调查"还是"追问"
    # 同一会话同一时刻只能处理一条消息。否则两条消息的历史会交错，把对话搞乱。
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def total_input_tokens(self) -> int:
        return sum(t.input_tokens for t in self.turns)

    @property
    def total_output_tokens(self) -> int:
        return sum(t.output_tokens for t in self.turns)


class SessionUnavailable(Exception):
    """会话存在，但现在没法恢复它（比如它用过知识库检索，而知识库现在不可用）。"""


class SessionConflict(Exception):
    """保存失败：会话在这期间被别人改过，或者我们的租约已经过期被别人接手。"""


class SessionTooLarge(Exception):
    pass


class SessionStorage(Protocol):
    """会话存储的接口。接口层（app/api/sessions.py）只依赖它，不关心背后是内存还是数据库。"""

    async def create(
        self,
        scenario_name: str,
        ticket_id: str,
        registry: ToolRegistry,
        owner: str,
        uses_knowledge: bool = False,
        agent_variant: str = "v1",
    ) -> Session: ...

    async def get(self, session_id: str) -> Session | None: ...

    async def is_busy(self, session: Session) -> bool: ...

    async def acquire(self, session: Session) -> bool:
        """占用会话来处理一条消息。拿到返回 True（并把 session 刷新成最新状态）；正忙返回 False，不排队。"""
        ...

    async def release(self, session: Session) -> None: ...

    async def save(self, session: Session) -> None:
        """一轮成功结束后保存。必须持有租约。"""
        ...


# ---------------------------------------------------------------------------
# 内存实现
# ---------------------------------------------------------------------------
class SessionStore:
    def __init__(self, max_sessions: int = 100):
        # OrderedDict 记住插入顺序，满了就淘汰最早创建的（类似 Java 的 LinkedHashMap）。
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._max_sessions = max_sessions

    async def create(
        self,
        scenario_name: str,
        ticket_id: str,
        registry: ToolRegistry,
        owner: str = "anonymous-dev",
        uses_knowledge: bool = False,
        agent_variant: str = "v1",
    ) -> Session:
        # 会话 id 必须不可猜（纵深防御；真正的访问控制是 owner 检查，见 app/api/sessions.py 的 _find_session）。
        # secrets 模块生成的是密码学安全的随机数；random 模块不行，它的输出是可预测的。
        session = Session(
            secrets.token_urlsafe(16), scenario_name, ticket_id, registry, owner,
            uses_knowledge=uses_knowledge, agent_variant=agent_variant,
        )
        self._sessions[session.id] = session
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)  # 淘汰最旧的
        return session

    async def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def peek(self, session_id: str) -> Session | None:
        """同步读取（测试里检查内部状态用）。"""
        return self._sessions.get(session_id)

    async def is_busy(self, session: Session) -> bool:
        return session.lock.locked()

    async def acquire(self, session: Session) -> bool:
        # 检查"是否正忙"和"获取锁"之间没有 await，asyncio 是单线程协作式调度，
        # 所以这两步之间不会被别的请求插队，不会出现"都以为自己拿到了锁"。
        # 选择直接返回"忙"（409）而不是排队等待：避免请求越堆越多，也让前端明确知道要等一下。
        if session.lock.locked():
            return False
        await session.lock.acquire()
        return True

    async def release(self, session: Session) -> None:
        if session.lock.locked():
            session.lock.release()

    async def save(self, session: Session) -> None:
        pass  # 对象本身就在内存里，已经是最新的


# ---------------------------------------------------------------------------
# 数据库实现
# ---------------------------------------------------------------------------
RegistryFactory = Callable[[str, bool, str], Awaitable[ToolRegistry]]  # (场景, 是否用知识库, agent 配置)
_TURNS = TypeAdapter(list[Turn])

_COLUMNS = "id, owner, scenario, ticket_id, uses_knowledge, messages, turns, report, version, agent_variant"


class PostgresSessionStore:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        registry_factory: RegistryFactory,
        *,
        max_per_owner: int = 100,
        lease_seconds: float = 180.0,
        max_bytes: int = 5_000_000,
    ):
        self._pool = pool
        self._registry_factory = registry_factory
        self._max_per_owner = max_per_owner
        self._lease_seconds = lease_seconds
        self._max_bytes = max_bytes

    async def _to_session(self, row: tuple, lease_token: str | None = None) -> Session:
        sid, owner, scenario, ticket_id, uses_knowledge, messages, turns, report, version, agent_variant = row
        # 工具不能存进数据库（它们是代码），按创建时的方式重建；重建不出来（比如知识库现在不可用）就抛 SessionUnavailable。
        registry = await self._registry_factory(scenario, uses_knowledge, agent_variant)
        return Session(
            id=sid,
            scenario_name=scenario,
            ticket_id=ticket_id,
            registry=registry,
            owner=owner,
            uses_knowledge=uses_knowledge,
            agent_variant=agent_variant,
            version=version,
            lease_token=lease_token,
            messages=[message_from_json(m) for m in messages],
            turns=_TURNS.validate_python(turns),
            report=RCAReport.model_validate(report) if report is not None else None,
        )

    async def create(
        self,
        scenario_name: str,
        ticket_id: str,
        registry: ToolRegistry,
        owner: str = "anonymous-dev",
        uses_knowledge: bool = False,
        agent_variant: str = "v1",
    ) -> Session:
        session_id = secrets.token_urlsafe(16)
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO chat_sessions (id, owner, scenario, ticket_id, uses_knowledge, agent_variant) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (session_id, owner, scenario_name, ticket_id, uses_knowledge, agent_variant),
            )
            # 每个用户最多保留 max_per_owner 个，超出的最旧的删掉（和内存版"淘汰最旧"的语义一致，但不会让一个用户挤掉别人的）。
            await conn.execute(
                "DELETE FROM chat_sessions WHERE owner = %s AND id NOT IN "
                "(SELECT id FROM chat_sessions WHERE owner = %s ORDER BY updated_at DESC, created_at DESC LIMIT %s)",
                (owner, owner, self._max_per_owner),
            )
        return Session(
            session_id, scenario_name, ticket_id, registry, owner, uses_knowledge=uses_knowledge, agent_variant=agent_variant
        )

    async def get(self, session_id: str) -> Session | None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(f"SELECT {_COLUMNS} FROM chat_sessions WHERE id = %s", (session_id,))
            ).fetchone()
        return await self._to_session(row) if row else None

    async def is_busy(self, session: Session) -> bool:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute("SELECT busy_until > now() FROM chat_sessions WHERE id = %s", (session.id,))
            ).fetchone()
        return bool(row and row[0])

    async def acquire(self, session: Session) -> bool:
        """一条语句同时完成"抢租约"和"读最新状态"：
        抢到的话，拿到的一定是抢到那一刻的最新历史，不会在"先读、后抢"之间被别人改掉。"""
        token = secrets.token_urlsafe(12)
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    f"""
                    UPDATE chat_sessions
                    SET busy_until = now() + make_interval(secs => %s), busy_token = %s
                    WHERE id = %s AND (busy_until IS NULL OR busy_until <= now())
                    RETURNING {_COLUMNS}
                    """,
                    (self._lease_seconds, token, session.id),
                )
            ).fetchone()
        if row is None:
            return False
        fresh = await self._to_session(row, lease_token=token)
        # 原地刷新调用方手里的对象：它之前可能是几百毫秒前读到的旧状态。
        session.messages, session.turns, session.report = fresh.messages, fresh.turns, fresh.report
        session.version, session.lease_token, session.registry = fresh.version, token, fresh.registry
        return True

    async def release(self, session: Session) -> None:
        if session.lease_token is None:
            return
        async with self._pool.connection() as conn:
            # 只释放【自己的】租约：如果租约已过期并被别人接手，这条语句什么也不会改，不会把别人的租约清掉。
            await conn.execute(
                "UPDATE chat_sessions SET busy_until = NULL, busy_token = NULL WHERE id = %s AND busy_token = %s",
                (session.id, session.lease_token),
            )
        session.lease_token = None

    async def save(self, session: Session) -> None:
        messages = [message_to_json(m) for m in session.messages]
        turns = [t.model_dump(mode="json") for t in session.turns]
        report = session.report.model_dump(mode="json") if session.report is not None else None
        size = len(json.dumps(messages)) + len(json.dumps(turns))
        if size > self._max_bytes:
            raise SessionTooLarge(f"session content is {size} bytes")
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE chat_sessions
                SET messages = %s, turns = %s, report = %s, version = version + 1, updated_at = now()
                WHERE id = %s AND busy_token = %s AND version = %s
                """,
                (Jsonb(messages), Jsonb(turns), Jsonb(report), session.id, session.lease_token, session.version),
            )
        if cursor.rowcount != 1:
            raise SessionConflict("the session was changed by someone else, or the lease expired")
        session.version += 1

    async def purge_expired(self, ttl_days: float) -> int:
        """删掉长期没有活动的会话。对话里含日志等运行数据，不应该永久保留。"""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM chat_sessions WHERE updated_at < now() - make_interval(secs => %s)",
                (ttl_days * 86400,),
            )
            return cursor.rowcount


def make_registry_factory(get_knowledge: Callable[[], Awaitable[object | None]]) -> RegistryFactory:
    """生成"恢复会话时重建工具"的函数。get_knowledge 返回当前可用的知识库（没有就返回 None）。"""

    async def build(scenario_name: str, uses_knowledge: bool, agent_variant: str = "v1") -> ToolRegistry:
        try:
            scenario = load_scenario(scenario_name)
        except (FileNotFoundError, ValueError):
            raise SessionUnavailable(f"scenario {scenario_name!r} is no longer available") from None
        knowledge = None
        if uses_knowledge:
            knowledge = await get_knowledge()
            if knowledge is None:
                # 这个会话的历史里已经有知识库检索的调用，工具集必须和当时一致；
                # 现在知识库不可用的话，宁可明确说"暂时恢复不了"，也不要悄悄换一套工具继续对话。
                raise SessionUnavailable("the knowledge base this session used is not available")
        return build_investigation_tools(scenario, knowledge, service_discovery=AGENT_VARIANTS[agent_variant].service_discovery)

    return build


async def release_shielded(store: SessionStorage, session: Session) -> None:
    """在"请求被取消"的情况下也能把租约释放掉。

    客户端断开时，Web 框架会取消正在处理请求的任务。被取消的任务里，之后每一个 await 都会立刻再次被取消，
    于是 finally 里的"释放租约"这条数据库调用根本发不出去，会话要等租约到期才能再用。
    shield=True 的取消范围让这一小段不受外层取消影响。内存版的释放没有 await，不受影响，所以以前没出过这个问题。
    """
    with anyio.CancelScope(shield=True):
        try:
            await store.release(session)
        except Exception:
            logger.exception("could not release the session lease; it will expire on its own")


def get_session_store(request: Request) -> SessionStorage:
    # 由应用启动时创建（见 main.py 的 lifespan），通过依赖注入交给接口；测试里可以替换成干净的实例。
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        raise RuntimeError("session store is not initialised")
    return store
