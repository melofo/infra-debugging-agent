"""写操作的人工审批流程测试（真实 PostgreSQL，没启动时自动跳过）。

覆盖的是"出了问题会怎样"：重复批准、并发批准、内容被改、过期、外部系统失败、权限不够。
正常路径只是其中一小部分。
"""

import asyncio
import re
import uuid

import httpx
import psycopg
import pytest

from app import actions, auth, jobs
from app.actions import neutralize, render_comment
from app.api.actions import get_jira_writer
from app.config import Settings
from app.db import get_pool
from app.main import app
from app.schemas import RCAReport
from app.worker import Worker
from tests.fakes import AutoLLM, JIRA_EVIDENCE, report_args

JOB_ID = uuid.uuid4()


# ============================================================ 内容：把不可信的文字变成纯文本（不需要数据库）
@pytest.mark.parametrize(
    "hostile",
    [
        "[~accountid:abc123] please look",  # 会通知某个人
        "[click here|http://evil.example.com]",  # 钓鱼链接
        "{code}malicious{code}",  # 宏
        "{noformat}x{noformat}",
        "a | b | c",  # 表格语法
    ],
)
def test_markup_that_would_act_on_the_target_system_is_neutralized(hostile):
    cleaned = neutralize(hostile)
    assert not re.search(r"[\[\]{}|]", cleaned)


def test_control_and_invisible_characters_are_removed():
    # 零宽空格、从右到左覆盖：能让"审批人看到的"和"实际发出去的"不一样
    assert neutralize("ab​c‮def\x00g\x1b") == "abcdefg"


def test_newlines_are_flattened_and_length_is_capped():
    assert neutralize("line one\n\n  line two\r\nthree") == "line one line two three"
    long = neutralize("x" * 5000, limit=100)
    assert len(long) == 100 and long.endswith("…")


def make_report(**overrides) -> RCAReport:
    args = report_args(JIRA_EVIDENCE)
    args["hypotheses"][0].update(overrides)
    return RCAReport(incident_id="BUG-1042", **args)


def test_the_comment_is_built_from_a_fixed_template_with_clean_fields():
    report = make_report(
        root_cause="pool shrank [~admin] {code}x{code}",
        suggested_next_steps=["roll back [link|http://evil.example.com]"],
    )
    body = render_comment(report, JOB_ID)

    assert body.startswith("Automated root cause analysis (AI-generated, approved by a human")
    assert f"Investigation id: {JOB_ID}" in body
    assert "confidence 90%" in body and "[jira] BUG-1042" in body
    # 模板自己带的 "[jira]" 是我们写的；模型写的字段里不能再有任何标记字符
    assert "[~admin]" not in body and "{code}" not in body and "evil.example.com]" not in body


def test_the_comment_is_capped_and_lists_at_most_five_evidence_items():
    many = [{"source": "jira", "summary": "s", "reference": f"BUG-{n:04d}"} for n in range(12)]
    body = render_comment(make_report(evidence=many, suggested_next_steps=["x" * 600] * 20), JOB_ID)
    assert body.count("- [jira]") == 5
    assert len(body) <= actions.MAX_COMMENT_CHARS


def test_the_content_hash_covers_both_the_ticket_and_the_text():
    base = actions.content_hash("BUG-1", "hello")
    assert base == actions.content_hash("BUG-1", "hello")
    assert base != actions.content_hash("BUG-2", "hello") and base != actions.content_hash("BUG-1", "hello!")


# ============================================================ 夹具
@pytest.fixture
async def system(pool, monkeypatch):
    """认证打开、数据库里有真实的密钥；返回 (客户端, 各角色的密钥)。"""
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE api_keys")
    monkeypatch.setattr("app.auth.get_settings", lambda: Settings(_env_file=None, auth_required=True))
    had, old = hasattr(app.state, "pool"), getattr(app.state, "pool", None)
    app.state.pool = pool
    app.dependency_overrides[get_pool] = lambda: pool
    keys = {
        "viewer": (await auth.create_key(pool, "vera", "viewer"))[0],
        "operator": (await auth.create_key(pool, "oscar", "operator"))[0],
        "admin": (await auth.create_key(pool, "alice", "admin"))[0],
        "admin2": (await auth.create_key(pool, "bob", "admin"))[0],
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, keys
    app.dependency_overrides.clear()
    if had:
        app.state.pool = old
    else:
        del app.state.pool


def bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def make_pending(pool, event_id="evt-1") -> actions.Action:
    """用真实的 Worker 跑完一个任务，让它自己产出待审批的提议（而不是手工插一行）。"""
    await jobs.enqueue(pool, event_id, "BUG-1042", "bug-1042")
    await Worker(pool, AutoLLM(), Settings(_env_file=None), worker_id="w1").run_once()
    [action] = [a for a in await actions.list_actions(pool) if a.status == "pending"]
    return action


async def outbox(pool) -> list[tuple]:
    async with pool.connection() as conn:
        return await (await conn.execute("SELECT ticket_id, body, idempotency_key FROM jira_outbox")).fetchall()


async def clear_outbox(pool):
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE jira_outbox")


@pytest.fixture(autouse=True)
async def _clean_outbox(pool):
    await clear_outbox(pool)


async def approve(client, key, action, **body):
    payload = {"content_hash": action.content_hash, **body}
    return await client.post(f"/actions/{action.id}/approve", json=payload, headers=bearer(key))


async def audit_actions(pool, action_id) -> list[tuple[str, str]]:
    return [(e["actor"], e["action"]) for e in reversed(await actions.list_audit(pool, 100, str(action_id)))]


# ============================================================ 提议是怎么产生的
async def test_a_successful_investigation_leaves_exactly_one_pending_proposal(pool):
    action = await make_pending(pool)

    assert (action.status, action.kind, action.ticket_id, action.proposed_by) == ("pending", "jira_comment", "BUG-1042", "agent")
    assert action.content_hash == actions.content_hash(action.ticket_id, action.body)
    assert "Automated root cause analysis" in action.body
    assert await outbox(pool) == []  # 提议不会产生任何副作用
    assert await audit_actions(pool, action.id) == [("agent", "proposed")]


async def test_proposing_twice_for_the_same_job_creates_one_proposal(pool):
    action = await make_pending(pool)
    job = (await jobs.list_jobs(pool))[0]
    again = await actions.propose_jira_comment(pool, job.id, "BUG-1042", RCAReport(incident_id="BUG-1042", **report_args(JIRA_EVIDENCE)), 24)
    assert again is None and len(await actions.list_actions(pool)) == 1
    assert (await audit_actions(pool, action.id)) == [("agent", "proposed")]  # 重复提议不重复记录


async def test_a_failed_investigation_proposes_nothing(pool):
    await jobs.enqueue(pool, "evt-bad", "BUG-9999", "does-not-exist")
    await Worker(pool, AutoLLM(), Settings(_env_file=None), worker_id="w1").run_once()
    assert await actions.list_actions(pool) == []


async def test_proposals_can_be_switched_off(pool):
    await jobs.enqueue(pool, "evt-off", "BUG-1042", "bug-1042")
    settings = Settings(_env_file=None, writeback_proposals_enabled=False)
    await Worker(pool, AutoLLM(), settings, worker_id="w1").run_once()
    assert await actions.list_actions(pool) == []


async def test_a_broken_proposal_step_does_not_fail_the_investigation(pool, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("proposal store down")

    monkeypatch.setattr(actions, "propose_jira_comment", boom)
    await jobs.enqueue(pool, "evt-x", "BUG-1042", "bug-1042")
    await Worker(pool, AutoLLM(), Settings(_env_file=None), worker_id="w1").run_once()
    assert (await jobs.list_jobs(pool))[0].status == "succeeded"


# ============================================================ 权限
async def test_who_may_see_and_who_may_decide(system, pool):
    client, keys = system
    action = await make_pending(pool)

    assert (await client.get("/actions")).status_code == 401
    for role in ("viewer", "operator", "admin"):
        assert (await client.get("/actions", headers=bearer(keys[role]))).status_code == 200
        assert (await client.get(f"/actions/{action.id}", headers=bearer(keys[role]))).status_code == 200
    for role in ("viewer", "operator"):
        assert (await approve(client, keys[role], action)).status_code == 403
        assert (await client.post(f"/actions/{action.id}/reject", json={}, headers=bearer(keys[role]))).status_code == 403
        assert (await client.get("/audit", headers=bearer(keys[role]))).status_code == 403
    assert (await actions.get_action(pool, action.id)).status == "pending"  # 没权限的请求没有改变任何东西
    assert await outbox(pool) == []


# ============================================================ 批准并执行
async def test_approving_posts_exactly_the_reviewed_text_and_records_who_did_it(system, pool):
    client, keys = system
    action = await make_pending(pool)

    response = await approve(client, keys["admin"], action, note="looks right")

    body = response.json()
    assert response.status_code == 200 and body["status"] == "executed"
    assert (body["decided_by"], body["decision_note"], body["executed_at"] is not None) == ("alice", "looks right", True)
    assert await outbox(pool) == [("BUG-1042", action.body, f"action-{action.id}")]
    assert await audit_actions(pool, action.id) == [
        ("agent", "proposed"),
        ("alice", "approved"),
        ("alice", "execution_started"),
        ("alice", "executed"),
    ]


async def test_a_wrong_or_stale_hash_is_rejected_and_nothing_changes(system, pool):
    client, keys = system
    action = await make_pending(pool)
    response = await client.post(
        f"/actions/{action.id}/approve", json={"content_hash": "0" * 64}, headers=bearer(keys["admin"])
    )
    assert response.status_code == 409 and "changed since you reviewed" in response.json()["detail"]
    assert (await actions.get_action(pool, action.id)).status == "pending"
    assert await outbox(pool) == []


async def test_a_malformed_hash_is_a_validation_error(system, pool):
    client, keys = system
    action = await make_pending(pool)
    response = await client.post(f"/actions/{action.id}/approve", json={"content_hash": "abc"}, headers=bearer(keys["admin"]))
    assert response.status_code == 422


async def test_approving_twice_posts_once(system, pool):
    client, keys = system
    action = await make_pending(pool)
    assert (await approve(client, keys["admin"], action)).status_code == 200
    second = await approve(client, keys["admin2"], action)
    assert second.status_code == 409 and "already executed" in second.json()["detail"]
    assert len(await outbox(pool)) == 1


async def test_two_people_approving_at_the_same_moment_post_once(system, pool):
    client, keys = system
    action = await make_pending(pool)
    results = await asyncio.gather(approve(client, keys["admin"], action), approve(client, keys["admin2"], action))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert len(await outbox(pool)) == 1
    audited = [a for _, a in await audit_actions(pool, action.id)]
    assert audited.count("approved") == 1 and audited.count("executed") == 1


async def test_an_agent_proposal_cannot_be_approved_by_its_own_proposer(system, pool):
    """职责分离：提议者不能批准自己的提议。这里把提议者改成 alice，再让 alice 批准。"""
    client, keys = system
    action = await make_pending(pool)
    async with pool.connection() as conn:
        await conn.execute("UPDATE proposed_actions SET proposed_by = 'alice' WHERE id = %s", (action.id,))
    assert (await approve(client, keys["admin"], action)).status_code == 409
    assert (await approve(client, keys["admin2"], action)).status_code == 200  # 另一个人可以


# ============================================================ 拒绝
async def test_rejecting_blocks_any_later_approval(system, pool):
    client, keys = system
    action = await make_pending(pool)
    rejected = await client.post(f"/actions/{action.id}/reject", json={"note": "wrong cause"}, headers=bearer(keys["admin"]))
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert (await approve(client, keys["admin2"], action)).status_code == 409
    assert await outbox(pool) == []
    assert [a for _, a in await audit_actions(pool, action.id)] == ["proposed", "rejected"]


# ============================================================ 过期
async def test_an_expired_proposal_can_be_neither_approved_nor_rejected(system, pool):
    client, keys = system
    action = await make_pending(pool)
    async with pool.connection() as conn:
        await conn.execute("UPDATE proposed_actions SET expires_at = now() - interval '1 minute' WHERE id = %s", (action.id,))

    shown = (await client.get(f"/actions/{action.id}", headers=bearer(keys["admin"]))).json()
    assert shown["status"] == "pending" and shown["expired"] is True
    approved = await approve(client, keys["admin"], action)
    assert approved.status_code == 409 and "expired" in approved.json()["detail"]
    assert (await client.post(f"/actions/{action.id}/reject", json={}, headers=bearer(keys["admin"]))).status_code == 409
    assert await outbox(pool) == []


# ============================================================ 出错
class BrokenJira:
    async def add_comment(self, ticket_id, body, idempotency_key):
        raise ConnectionError("jira.internal.example:8443 refused the connection")


async def test_an_external_failure_is_recorded_without_leaking_details_and_is_not_retried_silently(system, pool):
    client, keys = system
    action = await make_pending(pool)
    app.dependency_overrides[get_jira_writer] = lambda: BrokenJira()
    try:
        response = await approve(client, keys["admin"], action)
    finally:
        app.dependency_overrides.pop(get_jira_writer, None)

    body = response.json()
    assert response.status_code == 200 and body["status"] == "failed"
    assert "internal.example" not in response.text  # 外部系统的细节只写日志
    assert await outbox(pool) == []
    assert [a for _, a in await audit_actions(pool, action.id)][-1] == "execution_failed"
    assert (await approve(client, keys["admin2"], action)).status_code == 409  # 失败后不能再点一次"批准"重来


async def test_content_changed_after_approval_is_detected_before_sending(system, pool):
    """批准之后、执行之前，库里的内容被人改了：执行前的核对必须发现，不能把没人看过的内容发出去。"""
    client, keys = system
    action = await make_pending(pool)

    class TamperThenWrite:
        async def add_comment(self, *args, **kwargs):  # 不应该被调用
            raise AssertionError("tampered content must not be sent")

    async with pool.connection() as conn:
        await conn.execute("UPDATE proposed_actions SET body = body || ' [injected]' WHERE id = %s", (action.id,))
    app.dependency_overrides[get_jira_writer] = lambda: TamperThenWrite()
    try:
        response = await approve(client, keys["admin"], action)  # 审批人拿到的哈希是原来的，库里的哈希列没变
    finally:
        app.dependency_overrides.pop(get_jira_writer, None)

    assert response.json()["status"] == "failed" and "no longer matches" in response.json()["error"]
    assert await outbox(pool) == []


async def test_executing_an_action_that_is_not_approved_is_refused(pool):
    action = await make_pending(pool)
    with pytest.raises(actions.ActionError) as error:
        await actions.execute(pool, action.id, actions.SimulatedJira(pool), actor="x")
    assert error.value.status == 409
    assert await outbox(pool) == []


async def test_the_simulated_jira_deduplicates_by_idempotency_key(pool):
    jira = actions.SimulatedJira(pool)
    await jira.add_comment("BUG-1", "hello", "key-1")
    await jira.add_comment("BUG-1", "hello", "key-1")
    await jira.add_comment("BUG-1", "hello", "key-2")
    assert len(await outbox(pool)) == 2


async def test_unknown_and_malformed_ids(system):
    client, keys = system
    assert (await client.get(f"/actions/{uuid.uuid4()}", headers=bearer(keys["admin"]))).status_code == 404
    assert (await approve_by_id(client, keys["admin"], uuid.uuid4())).status_code == 404
    assert (await client.get("/actions/not-a-uuid", headers=bearer(keys["admin"]))).status_code == 422


async def approve_by_id(client, key, action_id):
    return await client.post(f"/actions/{action_id}/approve", json={"content_hash": "a" * 64}, headers=bearer(key))


async def test_listing_can_filter_by_status(system, pool):
    client, keys = system
    action = await make_pending(pool)
    await approve(client, keys["admin"], action)
    pending = await client.get("/actions?status=pending", headers=bearer(keys["viewer"]))
    executed = await client.get("/actions?status=executed", headers=bearer(keys["viewer"]))
    assert pending.json() == [] and [a["id"] for a in executed.json()] == [str(action.id)]
    assert (await client.get("/actions?status=bogus", headers=bearer(keys["viewer"]))).status_code == 422


# ============================================================ 审计日志
async def test_the_audit_log_cannot_be_changed_or_emptied(pool):
    action = await make_pending(pool)
    async with pool.connection() as conn:
        for statement in ("UPDATE audit_log SET actor = 'someone-else'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                async with conn.transaction():
                    await conn.execute(statement)
    assert await audit_actions(pool, action.id) == [("agent", "proposed")]


async def test_the_audit_endpoint_is_admin_only_and_can_filter(system, pool):
    client, keys = system
    action = await make_pending(pool)
    await approve(client, keys["admin"], action)

    entries = (await client.get(f"/audit?target_id={action.id}", headers=bearer(keys["admin"]))).json()
    assert [e["action"] for e in reversed(entries)] == ["proposed", "approved", "execution_started", "executed"]
    assert (await client.get("/audit", headers=bearer(keys["operator"]))).status_code == 403
    assert (await client.get("/audit?limit=0", headers=bearer(keys["admin"]))).status_code == 422


async def test_an_executed_or_rejected_action_cannot_be_rejected_again(system, pool):
    """已经发出去的评论不能被"拒绝"改写成 rejected：那会让记录和事实不符。"""
    client, keys = system
    action = await make_pending(pool)
    await approve(client, keys["admin"], action)

    late = await client.post(f"/actions/{action.id}/reject", json={"note": "too late"}, headers=bearer(keys["admin2"]))

    assert late.status_code == 409 and "already executed" in late.json()["detail"]
    assert (await actions.get_action(pool, action.id)).status == "executed"
    assert [a for _, a in await audit_actions(pool, action.id)].count("rejected") == 0
