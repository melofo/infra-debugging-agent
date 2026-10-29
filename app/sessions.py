"""会话存储：一个会话 = 一次调查 + 之后所有的追问。

【状态放在服务端】会话的完整历史（含工具结果）只保存在这里，浏览器只持有一个会话 id。
好处：请求小；工具结果可能很大或含敏感数据，不需要让客户端搬运；
更重要的是客户端无法篡改历史（否则有人可以伪造"工具返回了什么"来诱导模型）。

【当前是内存实现】进程重启就丢，多个实例之间也不共享。
这是刻意的简化，Phase 4 引入 PostgreSQL 时会换成持久化存储，接口（create/get）保持不变。
"""

import asyncio
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache

from pydantic import BaseModel

from app.agent.common import StepRecord
from app.agent.tools import ToolRegistry
from app.config import get_settings
from app.llm.base import Message
from app.schemas import RCAReport


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


class SessionStore:
    def __init__(self, max_sessions: int = 100):
        # OrderedDict 记住插入顺序，满了就淘汰最早创建的（类似 Java 的 LinkedHashMap）。
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._max_sessions = max_sessions

    def create(self, scenario_name: str, ticket_id: str, registry: ToolRegistry) -> Session:
        # 会话 id 必须不可猜：目前还没有登录认证，知道 id 就等于有权访问这个会话。
        # secrets 模块生成的是密码学安全的随机数；random 模块不行，它的输出是可预测的。
        session = Session(secrets.token_urlsafe(16), scenario_name, ticket_id, registry)
        self._sessions[session.id] = session
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)  # 淘汰最旧的
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)


@lru_cache
def get_session_store() -> SessionStore:
    # 全局唯一的存储（单例）。通过 Depends 注入到接口里，测试时可以替换成干净的实例。
    return SessionStore(max_sessions=get_settings().max_sessions)
