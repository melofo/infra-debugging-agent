"""Jira Webhook 接口的测试。

测试里的 sign() 就是"模拟 Jira"：它用共享密钥给请求体签名，和真实的 Jira 做的事一样。
重点覆盖安全相关的边界：伪造、篡改、没配置密钥、空密钥攻击、超大请求。
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import jobs
from app.api.webhooks import verify_signature
from app.config import Settings
from app.db import get_pool
from app.main import app

SECRET = "test-secret"


def sign(body: bytes, secret: str = SECRET, method: str = "sha256") -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"{method}={digest}"


def issue_created(key: str = "BUG-1042", issue_type: str = "Bug", event: str = "jira:issue_created") -> bytes:
    payload = {
        "timestamp": 1760000000000,
        "webhookEvent": event,
        "issue": {"id": "10001", "key": key, "fields": {"summary": "Checkout 500s", "issuetype": {"name": issue_type}}},
        "user": {"displayName": "Someone"},  # Jira 会发很多我们不关心的字段，必须能容忍
    }
    return json.dumps(payload).encode()


@pytest.fixture
def client(pool, monkeypatch):
    """配好密钥和测试数据库的客户端。"""
    configured = Settings(_env_file=None, jira_webhook_secret=SECRET)
    monkeypatch.setattr("app.api.webhooks.get_settings", lambda: configured)
    app.dependency_overrides[get_pool] = lambda: pool
    yield TestClient(app)
    app.dependency_overrides.clear()


def post(client, body: bytes, signature: str | None = "auto", delivery_id: str | None = "delivery-1"):
    headers = {"Content-Type": "application/json"}
    if signature == "auto":
        signature = sign(body)
    if signature is not None:
        headers["X-Hub-Signature"] = signature
    if delivery_id is not None:
        headers["X-Atlassian-Webhook-Identifier"] = delivery_id
    return client.post("/webhooks/jira", content=body, headers=headers)


# ------------------------------------------------------------ 正常流程
async def test_signed_bug_created_event_is_queued(client, pool):
    response = post(client, issue_created())

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    [job] = await jobs.list_jobs(pool)
    assert (job.ticket_id, job.scenario, job.status, job.event_id) == ("BUG-1042", "bug-1042", "queued", "delivery-1")


async def test_redelivery_of_the_same_event_does_not_create_a_second_job(client, pool):
    first = post(client, issue_created(), delivery_id="same-delivery")
    second = post(client, issue_created(), delivery_id="same-delivery")  # Jira 重试：投递 id 不变

    assert first.status_code == 202
    assert (second.status_code, second.json()["status"]) == (200, "duplicate")
    assert second.json()["job_id"] == first.json()["job_id"]
    assert len(await jobs.list_jobs(pool)) == 1


async def test_without_a_delivery_id_the_body_hash_is_used_for_deduplication(client, pool):
    body = issue_created()
    post(client, body, delivery_id=None)
    again = post(client, body, delivery_id=None)

    assert again.json()["status"] == "duplicate"
    assert len(await jobs.list_jobs(pool)) == 1


# ------------------------------------------------------------ 签名验证
@pytest.mark.parametrize(
    "case, signature",
    [
        ("missing header", None),
        ("empty header", ""),
        ("wrong secret", sign(issue_created(), secret="attacker-guess")),
        ("wrong method prefix", sign(issue_created(), method="sha1")),
        ("no method prefix", hmac.new(SECRET.encode(), issue_created(), hashlib.sha256).hexdigest()),
        ("garbage", "sha256=not-hex"),
    ],
)
async def test_bad_signatures_are_rejected_and_nothing_is_queued(client, pool, case, signature):
    response = post(client, issue_created(), signature=signature)
    assert response.status_code == 401, case
    assert await jobs.list_jobs(pool) == []


async def test_a_tampered_body_fails_verification(client, pool):
    original = issue_created(key="BUG-1042")
    signature = sign(original)  # 攻击者截获了一个合法请求的签名……
    tampered = issue_created(key="BUG-9999")  # ……然后改了内容再发
    response = post(client, tampered, signature=signature)

    assert response.status_code == 401
    assert await jobs.list_jobs(pool) == []


async def test_signature_is_checked_before_the_body_is_parsed(client):
    # 内容无法解析、签名也不对：应该先报签名错误（401），而不是内容错误（400）。
    # 否则攻击者可以不通过认证就探测我们的解析行为。
    response = post(client, b"this is not json", signature="sha256=deadbeef")
    assert response.status_code == 401


def test_verify_signature_rejects_empty_secret_even_if_the_attacker_signs_with_an_empty_key():
    body = b"{}"
    forged = "sha256=" + hmac.new(b"", body, hashlib.sha256).hexdigest()
    assert verify_signature("", body, forged) is False  # 空密钥的 HMAC 谁都能伪造，必须拒绝


# ------------------------------------------------------------ 配置与可用性
def test_webhook_refuses_everything_when_no_secret_is_configured(pool, monkeypatch):
    # "失败时关闭"：忘了配置密钥，不能因此变成无需认证的入口。
    unconfigured = Settings(_env_file=None, jira_webhook_secret="")
    monkeypatch.setattr("app.api.webhooks.get_settings", lambda: unconfigured)
    app.dependency_overrides[get_pool] = lambda: pool
    try:
        body = issue_created()
        # 即使攻击者用空密钥"正确地"签了名，也必须拒绝。
        forged = "sha256=" + hmac.new(b"", body, hashlib.sha256).hexdigest()
        response = post(TestClient(app), body, signature=forged)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 503


def test_database_unavailable_returns_503_so_jira_will_retry(monkeypatch):
    configured = Settings(_env_file=None, jira_webhook_secret=SECRET)
    monkeypatch.setattr("app.api.webhooks.get_settings", lambda: configured)
    # 不覆盖 get_pool，也没有启动流程，所以没有连接池。
    response = post(TestClient(app), issue_created())
    # 503 属于 Jira 会重试的状态码：数据库恢复后，这个事件还会再送一次，不会丢。
    assert response.status_code == 503


async def test_oversized_body_is_rejected_before_signature_verification(client, pool, monkeypatch):
    small = Settings(_env_file=None, jira_webhook_secret=SECRET, webhook_max_body_bytes=100)
    monkeypatch.setattr("app.api.webhooks.get_settings", lambda: small)
    response = post(client, issue_created())  # 这个请求体超过 100 字节
    assert response.status_code == 413
    assert await jobs.list_jobs(pool) == []


# ------------------------------------------------------------ 事件过滤
@pytest.mark.parametrize(
    "body",
    [
        issue_created(issue_type="Task"),  # 不是 Bug
        issue_created(event="jira:issue_updated"),  # 不是"新建"
        issue_created(key="BUG-7777"),  # 我们没有这张工单的数据
    ],
    ids=["not-a-bug", "not-created", "unknown-ticket"],
)
async def test_events_we_do_not_handle_are_acknowledged_but_not_queued(client, pool, body):
    response = post(client, body)
    # 200 而不是 4xx：事件已成功送达，只是我们不处理，Jira 不需要重试。
    assert (response.status_code, response.json()["status"]) == (200, "ignored")
    assert await jobs.list_jobs(pool) == []


async def test_other_event_types_with_unfamiliar_shapes_are_ignored_not_rejected(client, pool):
    weird = json.dumps({"webhookEvent": "comment_created", "issue": {"unexpected": "shape"}}).encode()
    response = post(client, weird)
    assert (response.status_code, response.json()["status"]) == (200, "ignored")


async def test_signed_but_malformed_payload_returns_400(client, pool):
    bad_issue = json.dumps({"webhookEvent": "jira:issue_created", "issue": {"key": "BUG-1042"}}).encode()
    assert post(client, bad_issue).status_code == 400
    assert post(client, b"not json at all").status_code == 400
    assert await jobs.list_jobs(pool) == []


async def test_a_database_failure_during_enqueue_returns_503_not_500(client, pool):
    # 请求处理到一半数据库不可用（这里用"关闭连接池"模拟）：必须是明确的 503，Jira 会重试。
    await pool.close()
    response = post(client, issue_created())
    assert response.status_code == 503
    assert response.json()["detail"] == "database unavailable"  # 不泄露内部错误细节
