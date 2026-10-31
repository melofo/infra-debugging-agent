"""待审批的写操作：查看、批准、拒绝。

  GET  /actions                 待审批/已处理的提议列表            （jobs:read）
  GET  /actions/{id}            单个提议，含将要发出的确切内容       （jobs:read）
  POST /actions/{id}/approve    批准并执行。必须带上你看到的内容哈希   （approvals:decide，admin）
  POST /actions/{id}/reject     拒绝                              （approvals:decide，admin）
  GET  /audit                   审计日志                           （approvals:decide，admin）

读和写用不同的权限：能看见提议的人很多，能批准的人要少得多（最小权限）。
"""

import logging
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from app import actions
from app.actions import Action, ActionError, JiraWriter, SimulatedJira
from app.auth import Principal, require_scope
from app.db import get_pool

router = APIRouter()
logger = logging.getLogger(__name__)


def get_jira_writer(pool: AsyncConnectionPool = Depends(get_pool)) -> JiraWriter:
    # 通过依赖注入提供"写 Jira 的能力"：测试里换成会失败/会记录调用的假对象，真实部署里换成调用 Jira 的实现。
    return SimulatedJira(pool)


class ApproveRequest(BaseModel):
    content_hash: str = Field(min_length=64, max_length=64, description="the hash of the content you reviewed")
    note: str | None = Field(default=None, max_length=actions.MAX_NOTE_CHARS)


class RejectRequest(BaseModel):
    note: str | None = Field(default=None, max_length=actions.MAX_NOTE_CHARS)


class AuditEntry(BaseModel):
    id: int
    at: datetime
    actor: str
    action: str
    target_type: str
    target_id: str
    details: dict


def _raise(error: ActionError):
    raise HTTPException(status_code=error.status, detail=error.detail)


@router.get("/actions", response_model=list[Action])
async def list_actions(
    status: Literal["pending", "approved", "rejected", "executing", "executed", "failed"] | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    pool: AsyncConnectionPool = Depends(get_pool),
    _: Principal = Depends(require_scope("jobs:read")),
) -> list[Action]:
    return await actions.list_actions(pool, status, limit)


@router.get("/actions/{action_id}", response_model=Action)
async def get_action(
    action_id: UUID,
    pool: AsyncConnectionPool = Depends(get_pool),
    _: Principal = Depends(require_scope("jobs:read")),
) -> Action:
    action = await actions.get_action(pool, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="action not found")
    return action


@router.post("/actions/{action_id}/approve", response_model=Action)
async def approve_action(
    action_id: UUID,
    request: ApproveRequest,
    pool: AsyncConnectionPool = Depends(get_pool),
    writer: JiraWriter = Depends(get_jira_writer),
    principal: Principal = Depends(require_scope("approvals:decide")),
) -> Action:
    """批准并立即执行。返回最终状态：成功是 executed；执行出错是 failed（批准本身是成功的，错误在 error 字段里）。"""
    try:
        await actions.approve(pool, action_id, principal, request.content_hash, request.note)
        return await actions.execute(pool, action_id, writer, actor=principal.name)
    except ActionError as error:
        _raise(error)


@router.post("/actions/{action_id}/reject", response_model=Action)
async def reject_action(
    action_id: UUID,
    request: RejectRequest,
    pool: AsyncConnectionPool = Depends(get_pool),
    principal: Principal = Depends(require_scope("approvals:decide")),
) -> Action:
    try:
        return await actions.reject(pool, action_id, principal, request.note)
    except ActionError as error:
        _raise(error)


@router.get("/audit", response_model=list[AuditEntry])
async def list_audit(
    target_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    pool: AsyncConnectionPool = Depends(get_pool),
    _: Principal = Depends(require_scope("approvals:decide")),
) -> list[dict]:
    return await actions.list_audit(pool, limit, target_id)
