"""写操作的"提议 -> 人工审批 -> 执行"流程，以及审计日志。

    Agent / Worker --提议--> [pending] --人看过确切内容后批准--> [approved] --执行--> [executed]
                                 \\--拒绝--> [rejected]          \\--出错--> [failed]

【核心原则】
  1. Agent 永远没有写权限。它只能留下一个"提议"。真正的写操作由审批通过之后的系统代码执行。
     这样即使模型被提示词注入、被日志里的恶意文字带偏，最坏的结果也只是"一条待审批的提议"，不是"已经发出去的评论"。
  2. 批准的是"确切的内容"，不是"这个提议"：批准时要提交审批人看到的内容哈希，对不上就拒绝（防 TOCTOU）。
  3. 状态转换是数据库里的原子操作（UPDATE ... WHERE status = '旧状态'）：
     两个人同时点"批准"，或者重复点击，只有一个会成功，评论不会发两次。
  4. 每一步都写审计日志，且和状态变更在同一个事务里：不会出现"状态变了但没有记录"。
  5. 提议会过期；评论内容是模型从不可信的数据（日志、工单）里总结的，发出去之前要中和对目标系统有特殊含义的标记。
"""

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol
from uuid import UUID

import psycopg
from psycopg.rows import class_row, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.auth import Principal
from app.schemas import RCAReport

logger = logging.getLogger(__name__)

ActionStatus = Literal["pending", "approved", "rejected", "executing", "executed", "failed"]
MAX_COMMENT_CHARS = 4000
MAX_NOTE_CHARS = 500


class Action(BaseModel):
    id: UUID
    job_id: UUID
    kind: str
    ticket_id: str
    body: str
    content_hash: str
    status: ActionStatus
    proposed_by: str
    created_at: datetime
    expires_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str | None
    executed_at: datetime | None
    error: str | None
    expired: bool = False  # pending 且已过期（由查询算出来，不存数据库）


class ActionError(Exception):
    """流程上的错误（不存在、状态不对、内容不一致……），带 HTTP 状态码，由接口层转成响应。"""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class JiraWriter(Protocol):
    """"往 Jira 写评论"的能力。真实实现调用 Jira 的 REST 接口；这里只有模拟版。

    必须按 idempotency_key 去重：同一个 key 调用多次，只产生一条评论。
    """

    async def add_comment(self, ticket_id: str, body: str, idempotency_key: str) -> None: ...


class SimulatedJira:
    """把评论写进 jira_outbox 表。用唯一键去重，所以重复执行也只会有一条。"""

    def __init__(self, pool: AsyncConnectionPool):
        self._pool = pool

    async def add_comment(self, ticket_id: str, body: str, idempotency_key: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO jira_outbox (idempotency_key, ticket_id, body) VALUES (%s, %s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (idempotency_key, ticket_id, body),
            )


# ---------------------------------------------------------------------------
# 评论内容
# ---------------------------------------------------------------------------
def content_hash(ticket_id: str, body: str) -> str:
    return hashlib.sha256(f"{ticket_id}\n{body}".encode("utf-8")).hexdigest()


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Jira 的标记语法里有特殊含义的字符：[~user] 会通知某个人，[文字|网址] 是链接（钓鱼），{code}/{noformat} 是宏。
# 评论里的文字来自模型对日志、工单的总结，日志内容是外部可控的，所以不能让它在 Jira 里生效成标记。
_MARKUP = str.maketrans({"[": "(", "]": ")", "{": "(", "}": ")", "|": "/"})


def neutralize(text: str, limit: int = 600) -> str:
    """把不可信的文字变成纯文本：去掉控制字符，打平换行，去掉标记字符，限制长度。

    这是"输出编码"：数据要送进哪个系统，就要按那个系统的规则转义（和防 SQL 注入、防 XSS 是同一个思路）。
    """
    # 控制字符，以及不可见的格式字符（零宽字符、从右到左覆盖等）：它们能让审批人"看到的"和"实际发出去的"不一样。
    text = "".join(ch for ch in _CONTROL_CHARS.sub("", text) if unicodedata.category(ch) != "Cf")
    text = " ".join(text.split()).translate(_MARKUP)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_comment(report: RCAReport, job_id: UUID) -> str:
    """用【固定的模板】从结构化报告生成评论，而不是让模型自由写一段话。

    模板决定了评论里有哪些部分，模型只能填充字段（并且每个字段都被 neutralize 过）。
    审批人看到的就是这段文字，发出去的也是这段文字。
    """
    top = report.hypotheses[0]
    lines = [
        "Automated root cause analysis (AI-generated, approved by a human before posting).",
        f"Most likely cause (confidence {top.confidence:.0%}): {neutralize(top.root_cause)}",
        f"Summary: {neutralize(report.summary)}",
        "Evidence:",
    ]
    for evidence in top.evidence[:5]:
        lines.append(f"- [{evidence.source.value}] {neutralize(evidence.reference, 200)}")
    if top.suggested_next_steps:
        lines.append("Suggested next steps:")
        lines.extend(f"- {neutralize(step, 200)}" for step in top.suggested_next_steps[:5])
    lines.append(f"Investigation id: {job_id}")
    body = "\n".join(lines)
    return body if len(body) <= MAX_COMMENT_CHARS else body[: MAX_COMMENT_CHARS - 1] + "…"


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------
async def audit(conn: psycopg.AsyncConnection, actor: str, action: str, target_id: Any, details: dict | None = None) -> None:
    """写一条审计记录。传入的是调用方正在用的连接：和业务变更在同一个事务里，要么都成功要么都回滚。"""
    await conn.execute(
        "INSERT INTO audit_log (actor, action, target_type, target_id, details) VALUES (%s, %s, 'action', %s, %s)",
        (actor, action, str(target_id), Jsonb(details or {})),
    )


async def list_audit(pool: AsyncConnectionPool, limit: int = 100, target_id: str | None = None) -> list[dict]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, at, actor, action, target_type, target_id, details FROM audit_log "
            "WHERE (%s::text IS NULL OR target_id = %s) ORDER BY id DESC LIMIT %s",
            (target_id, target_id, limit),
        )
        return await cur.fetchall()


# ---------------------------------------------------------------------------
# 提议、查询
# ---------------------------------------------------------------------------
_SELECT = "SELECT *, (status = 'pending' AND expires_at <= now()) AS expired FROM proposed_actions"


async def propose_jira_comment(
    pool: AsyncConnectionPool, job_id: UUID, ticket_id: str, report: RCAReport, ttl_hours: float
) -> Action | None:
    """为一个成功的调查提议一条 Jira 评论。同一个任务只会有一个提议；已存在时返回 None。"""
    body = render_comment(report, job_id)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    async with pool.connection() as conn, conn.cursor(row_factory=class_row(Action)) as cur:
        await cur.execute(
            """
            INSERT INTO proposed_actions (job_id, kind, ticket_id, body, content_hash, expires_at)
            VALUES (%s, 'jira_comment', %s, %s, %s, %s)
            ON CONFLICT (job_id, kind) DO NOTHING
            RETURNING *, false AS expired
            """,
            (job_id, ticket_id, body, content_hash(ticket_id, body), expires_at),
        )
        action = await cur.fetchone()
        if action is not None:
            await audit(conn, "agent", "proposed", action.id, {"kind": "jira_comment", "ticket_id": ticket_id})
        return action


async def get_action(pool: AsyncConnectionPool, action_id: UUID) -> Action | None:
    async with pool.connection() as conn, conn.cursor(row_factory=class_row(Action)) as cur:
        await cur.execute(f"{_SELECT} WHERE id = %s", (action_id,))
        return await cur.fetchone()


async def list_actions(pool: AsyncConnectionPool, status: str | None = None, limit: int = 50) -> list[Action]:
    async with pool.connection() as conn, conn.cursor(row_factory=class_row(Action)) as cur:
        await cur.execute(
            f"{_SELECT} WHERE (%s::text IS NULL OR status = %s) ORDER BY created_at DESC LIMIT %s",
            (status, status, limit),
        )
        return await cur.fetchall()


# ---------------------------------------------------------------------------
# 决定（批准 / 拒绝）
# ---------------------------------------------------------------------------
async def _why_not_decidable(conn: psycopg.AsyncConnection, action_id: UUID, expected_hash: str | None) -> ActionError:
    """原子更新没有命中任何行时，查一下原因，给出准确的错误（用于调用方看到有意义的提示）。"""
    row = await (
        await conn.execute(
            "SELECT status, content_hash, expires_at <= now() FROM proposed_actions WHERE id = %s", (action_id,)
        )
    ).fetchone()
    if row is None:
        return ActionError(404, "action not found")
    status, stored_hash, expired = row
    if status != "pending":
        return ActionError(409, f"action is already {status}")
    if expired:
        return ActionError(409, "action has expired; ask for a new investigation")
    if expected_hash is not None and expected_hash != stored_hash:
        return ActionError(409, "the content changed since you reviewed it; reload and review it again")
    return ActionError(409, "action could not be updated")


async def approve(pool: AsyncConnectionPool, action_id: UUID, approver: Principal, expected_hash: str, note: str | None) -> Action:
    """批准一个提议。原子地 pending -> approved，条件全部写在同一条 UPDATE 里：
    状态仍是 pending、没过期、审批人看到的内容哈希和库里的一致。任何一条不满足就一行都不会更新。
    """
    async with pool.connection() as conn, conn.cursor(row_factory=class_row(Action)) as cur:
        await cur.execute(
            """
            UPDATE proposed_actions
            SET status = 'approved', decided_by = %s, decided_at = now(), decision_note = %s
            WHERE id = %s AND status = 'pending' AND expires_at > now() AND content_hash = %s
              AND proposed_by <> %s  -- 职责分离：提议者不能批准自己的提议
            RETURNING *, false AS expired
            """,
            (approver.name, (note or "")[:MAX_NOTE_CHARS] or None, action_id, expected_hash, approver.name),
        )
        action = await cur.fetchone()
        if action is None:
            raise await _why_not_decidable(conn, action_id, expected_hash)
        await audit(conn, approver.name, "approved", action.id, {"content_hash": action.content_hash})
        return action


async def reject(pool: AsyncConnectionPool, action_id: UUID, approver: Principal, note: str | None) -> Action:
    async with pool.connection() as conn, conn.cursor(row_factory=class_row(Action)) as cur:
        await cur.execute(
            """
            UPDATE proposed_actions
            SET status = 'rejected', decided_by = %s, decided_at = now(), decision_note = %s
            WHERE id = %s AND status = 'pending' AND expires_at > now()
            RETURNING *, false AS expired
            """,
            (approver.name, (note or "")[:MAX_NOTE_CHARS] or None, action_id),
        )
        action = await cur.fetchone()
        if action is None:
            raise await _why_not_decidable(conn, action_id, None)
        await audit(conn, approver.name, "rejected", action.id, {})
        return action


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
async def execute(pool: AsyncConnectionPool, action_id: UUID, writer: JiraWriter, actor: str) -> Action:
    """执行一个已批准的提议。

    1. 先原子地 approved -> executing："认领"这次执行。并发的第二次执行拿不到，不会重复发送。
    2. 执行前再核对一次：库里的内容哈希是否还等于批准时的哈希（防止批准之后被改过）。
    3. 调用外部系统（不在数据库事务里：外部调用可能很慢，不能一直占着连接和锁）。
    4. 记录结果。外部调用的异常细节只写日志，库里存的是通用的话。

    已知的限制：如果进程恰好在第 3 步中途崩溃，状态会停在 executing，需要人工核对 Jira 里到底有没有这条评论，
    不能盲目重试（可能发两次）。这里靠 idempotency_key 让重试在 Jira 端也是安全的，这是真实系统里的标准做法。
    """
    async with pool.connection() as conn, conn.cursor(row_factory=class_row(Action)) as cur:
        await cur.execute(
            "UPDATE proposed_actions SET status = 'executing' WHERE id = %s AND status = 'approved' "
            "RETURNING *, false AS expired",
            (action_id,),
        )
        action = await cur.fetchone()
        if action is None:
            raise ActionError(409, "action is not approved, or is already being executed")
        await audit(conn, actor, "execution_started", action.id, {})

    error: str | None = None
    if content_hash(action.ticket_id, action.body) != action.content_hash:
        error = "stored content no longer matches the approved hash"
        logger.error("action content tampered", extra={"action_id": str(action.id)})
    else:
        try:
            await writer.add_comment(action.ticket_id, action.body, idempotency_key=f"action-{action.id}")
        except Exception:
            logger.exception("action execution failed", extra={"action_id": str(action.id)})
            error = "execution failed (see server logs)"

    async with pool.connection() as conn, conn.cursor(row_factory=class_row(Action)) as cur:
        await cur.execute(
            """
            UPDATE proposed_actions
            SET status = %s, error = %s, executed_at = CASE WHEN %s THEN now() ELSE NULL END
            WHERE id = %s AND status = 'executing'
            RETURNING *, false AS expired
            """,
            ("failed" if error else "executed", error, error is None, action_id),
        )
        final = await cur.fetchone()
        assert final is not None
        await audit(conn, actor, "execution_failed" if error else "executed", final.id, {"error": error} if error else {})
        return final
