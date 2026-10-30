"""Jira Webhook 接收端：POST /webhooks/jira

Webhook = 对方（Jira）在事件发生时，主动向我们的一个 URL 发 HTTP 请求。
这个 URL 是公开的，任何人都能向它发请求，所以必须解决几个问题：

  1. 这个请求真的来自 Jira 吗？          -> HMAC 签名验证（先验证，再做任何别的事）
  2. Jira 可能重复投递同一个事件           -> 用投递 id 去重（幂等），重复不再创建任务
  3. 调查要 20 秒，而 Jira 只等 20 秒超时   -> 只入队，立刻返回 202，后台去做
  4. 返回什么状态码，决定 Jira 会不会重试   -> 按"重试语义"精心选择（见下表）

【状态码和 Jira 的重试行为】（Jira 对 408/409/425/429/5xx 会重试，最多 5 次；其他 4xx 不重试）
    202  新任务已入队              成功，不重试
    200  重复事件 / 不关心的事件    成功，不重试（返回 4xx 也不会重试，但 200 语义更准确）
    401  签名不对                  不重试。签名错误重试一万次也不会变对
    400  内容无法解析              不重试
    413  请求体太大                不重试
    503  数据库暂时不可用 / 未配置密钥   会重试。这正是我们想要的：等数据库恢复后，Jira 会再发一次
"""

import hashlib
import hmac
import json
import logging
from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.db import get_pool
from app.jobs import enqueue
from app.scenario import scenario_exists

router = APIRouter()
logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Hub-Signature"
DELIVERY_ID_HEADER = "X-Atlassian-Webhook-Identifier"


def verify_signature(secret: str, raw_body: bytes, header_value: str | None) -> bool:
    """验证 Jira 的 HMAC 签名。header 格式：sha256=<十六进制摘要>。

    【HMAC 是什么】对"消息 + 共享密钥"算一个哈希。只有同时知道密钥和消息的人才能算出正确的值：
    Jira 用密钥给请求体签名；我们用同一个密钥重新算一遍，一致就说明请求来自 Jira 且内容没被改过。
    密钥只在两边保存，从不在网络上传输。

    【两个安全细节】
    1. 必须对【原始请求体字节】签名验证，不能先解析成 JSON 再序列化回去验。
       JSON 解析再序列化后，空格、字段顺序、转义都可能变化，哪怕内容等价，字节也不同，签名就对不上。
    2. 比较用 hmac.compare_digest（常量时间比较），不能用 ==。
       == 在遇到第一个不同的字符时就提前返回，攻击者可以通过测量响应时间的细微差别，
       一个字节一个字节地猜出正确的签名，这叫"时序攻击"（timing attack）。
       compare_digest 无论哪里不同，耗时都一样。
    """
    if not secret or not header_value:
        return False
    method, _, provided = header_value.partition("=")
    # Atlassian 文档提醒将来可能更换签名方法，所以必须检查前缀：只接受 sha256，别的一律拒绝。
    if method != "sha256":
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


# 只声明我们需要的字段。Pydantic 默认忽略多余字段，所以 Jira 以后加了新字段也不会让解析失败。
class _IssueType(BaseModel):
    name: str


class _IssueFields(BaseModel):
    issuetype: _IssueType


class _Issue(BaseModel):
    key: str
    fields: _IssueFields


class JiraEvent(BaseModel):
    webhookEvent: str
    # 先只当作普通字典接收。只有确认是"新建工单"事件之后，才按 _Issue 严格解析：
    # 其他类型的事件（评论、更新……）的 issue 结构可能不同，不该因为我们不关心的内容而报 400。
    issue: dict | None = None


class WebhookAccepted(BaseModel):
    status: Literal["queued", "duplicate", "ignored"]
    job_id: str | None = None
    reason: str | None = None


@router.post("/webhooks/jira", response_model=WebhookAccepted, status_code=202)
async def jira_webhook(
    request: Request,
    response: Response,
    pool: AsyncConnectionPool = Depends(get_pool),
) -> WebhookAccepted:
    settings = get_settings()

    # 【第 0 关】没配置密钥就彻底拒绝（"失败时关闭"，fail closed）。
    # 如果写成"没有密钥就跳过验证"，忘了配置密钥的部署就等于对全世界开放了触发入口。
    # 同时，空密钥也不能参与验证：用空密钥算出的 HMAC 谁都能伪造。
    if not settings.jira_webhook_secret:
        logger.error("jira webhook called but JIRA_WEBHOOK_SECRET is not configured")
        raise HTTPException(status_code=503, detail="webhook is not configured")

    # 【第 1 关】请求体大小。放在签名验证之前：不能让匿名请求逼我们对一个超大的包体做哈希计算。
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.webhook_max_body_bytes:
        raise HTTPException(status_code=413, detail="payload too large")
    raw_body = await request.body()  # 原始字节。用 request.body() 而不是让 FastAPI 自动解析 JSON
    if len(raw_body) > settings.webhook_max_body_bytes:  # Content-Length 可能缺失或撒谎，再核对实际大小
        raise HTTPException(status_code=413, detail="payload too large")

    # 【第 2 关】签名。通不过就到此为止：不解析内容、不碰数据库。
    # 注意：这之前不能有任何依赖请求内容的处理，因为内容在验证前都是不可信的。
    if not verify_signature(settings.jira_webhook_secret, raw_body, request.headers.get(SIGNATURE_HEADER)):
        logger.warning("jira webhook rejected: bad or missing signature")  # 不记录请求体和签名，避免泄露
        raise HTTPException(status_code=401, detail="invalid signature")

    # 【第 3 关】解析。到这里内容才是可信来源发的，但仍可能格式不对。
    try:
        event = JiraEvent.model_validate(json.loads(raw_body))
    except (ValueError, ValidationError):
        raise HTTPException(status_code=400, detail="malformed payload")

    # 只关心"新建 Bug"。别的事件（更新、评论、其他类型的工单）直接确认收到但不处理。
    # 返回 200 而不是错误：对 Jira 来说这个事件已经"成功送达"，不需要重试。
    if event.webhookEvent != "jira:issue_created" or event.issue is None:
        response.status_code = 200
        return WebhookAccepted(status="ignored", reason="event type not handled")
    try:
        issue = _Issue.model_validate(event.issue)
    except ValidationError:
        raise HTTPException(status_code=400, detail="malformed issue")
    if issue.fields.issuetype.name.lower() != "bug":
        response.status_code = 200
        return WebhookAccepted(status="ignored", reason="not a bug")

    ticket_id = issue.key
    scenario = ticket_id.lower()  # 我们的模拟数据按工单号命名：BUG-1042 -> data/scenarios/bug-1042.json
    if not scenario_exists(scenario):
        # 没有这张工单的数据可供调查。真实系统里这里会改为直接查询 Jira 和监控系统。
        response.status_code = 200
        return WebhookAccepted(status="ignored", reason="no investigation data for this ticket")

    # 【幂等】去重键优先用 Jira 的投递 id（重试时保持不变）。
    # 没有这个头时退而求其次：用请求体的哈希。同一次投递的重试，请求体字节完全相同，所以哈希也相同。
    delivery_id = request.headers.get(DELIVERY_ID_HEADER) or f"body-sha256:{hashlib.sha256(raw_body).hexdigest()}"

    try:
        job, created = await enqueue(pool, delivery_id, ticket_id, scenario)
    except psycopg.OperationalError:
        # 数据库连不上、借连接超时、连接池已关闭，都属于 OperationalError。
        # 明确返回 503（Jira 会重试），而不是让异常冒泡成 500：状态码应该是我们有意选择的，
        # 因为它直接决定了发送方的行为。只记录原因，不把内部错误细节返回给调用方。
        logger.exception("could not enqueue jira event")
        raise HTTPException(status_code=503, detail="database unavailable")
    logger.info(
        "jira webhook accepted",
        extra={"ticket_id": ticket_id, "job_id": str(job.id), "duplicate": not created},
    )
    if not created:
        response.status_code = 200
        return WebhookAccepted(status="duplicate", job_id=str(job.id))
    return WebhookAccepted(status="queued", job_id=str(job.id))
