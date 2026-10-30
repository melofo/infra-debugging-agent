"""查询自动调查任务的接口（让 Jira 触发的调查结果"看得见"）。

  GET /jobs          最近的任务列表（精简信息，用于列表页）
  GET /jobs/{id}     单个任务的完整详情，含 RCA 报告和调查步骤

这就是前面说的"异步请求-响应"里的"取餐窗口"：Webhook 返回 202 + job_id，
调查在后台做，做完之后工程师用这里的接口来取结果。
"""

import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ValidationError

from app import jobs
from app.agent.common import StepRecord
from app.auth import require_scope
from app.db import get_pool
from app.jobs import Job
from app.schemas import RCAReport

router = APIRouter(dependencies=[Depends(require_scope("jobs:read"))])
logger = logging.getLogger(__name__)

JobStatus = Literal["queued", "running", "succeeded", "failed"]


class JobResult(BaseModel):
    """worker 存进 result 字段的内容。结构必须和 app/worker.py 的 result_to_dict 保持一致。"""

    answer: str
    report: RCAReport
    steps: list[StepRecord]
    llm_calls: int
    input_tokens: int
    output_tokens: int
    latency_ms: int


class JobSummary(BaseModel):
    """列表页用的精简信息：不含完整报告，只带最可能的根因和置信度作为预览。"""

    id: UUID
    ticket_id: str
    status: JobStatus
    attempts: int
    max_attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    top_cause: str | None  # 排名第一的根因假设（仅成功的任务有）
    confidence: float | None


class JobDetail(JobSummary):
    scenario: str
    run_after: datetime  # 排队中的任务：最早什么时候会被执行（重试时是退避之后）
    result: JobResult | None


def _parse_result(job: Job) -> JobResult | None:
    """把 jsonb 里的字典解析成有类型的对象。

    库里的数据是旧版本代码写进去的，以后报告的结构可能演进，导致旧数据解析不了。
    这种情况不应该让整个列表接口报错，而是记录下来、当作"没有结果"处理。
    """
    if job.result is None:
        return None
    try:
        return JobResult.model_validate(job.result)
    except ValidationError:
        logger.warning("job result does not match the current schema", extra={"job_id": str(job.id)})
        return None


def _summary_fields(job: Job, result: JobResult | None) -> dict:
    top = result.report.hypotheses[0] if result and result.report.hypotheses else None
    return {
        "id": job.id,
        "ticket_id": job.ticket_id,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "last_error": job.last_error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "finished_at": job.finished_at,
        "top_cause": top.root_cause if top else None,
        "confidence": top.confidence if top else None,
    }


@router.get("/jobs", response_model=list[JobSummary])
async def list_jobs(
    # Query(ge, le)：限制取值范围，防止有人请求 limit=1000000 把整张表拖出来。
    limit: int = Query(default=50, ge=1, le=100),
    pool: AsyncConnectionPool = Depends(get_pool),
) -> list[JobSummary]:
    try:
        rows = await jobs.list_jobs(pool, limit)
    except psycopg.OperationalError:
        logger.exception("could not list jobs")
        raise HTTPException(status_code=503, detail="database unavailable")
    return [JobSummary(**_summary_fields(job, _parse_result(job))) for job in rows]


@router.get("/jobs/{job_id}", response_model=JobDetail)
async def get_job(job_id: UUID, pool: AsyncConnectionPool = Depends(get_pool)) -> JobDetail:
    # 路径参数声明成 UUID 类型：不是合法 UUID 的请求，FastAPI 自动返回 422，不会进到这里查数据库。
    try:
        job = await jobs.get_job(pool, job_id)
    except psycopg.OperationalError:
        logger.exception("could not load job")
        raise HTTPException(status_code=503, detail="database unavailable")
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    result = _parse_result(job)
    return JobDetail(**_summary_fields(job, result), scenario=job.scenario, run_after=job.run_after, result=result)
