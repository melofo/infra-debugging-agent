"""接口认证与授权的测试（真实 PostgreSQL，没启动时自动跳过）。

用 httpx.AsyncClient + ASGITransport 直接在当前事件循环里调用应用：
连接池是在测试的事件循环里打开的，不能被另一个线程的事件循环（TestClient 的做法）使用。

测试里的 conftest 默认关闭认证；这里的 `secured` fixture 把它打开，
并用真实的数据库校验真实生成的密钥（不 mock 校验逻辑，否则测的就是 mock 本身）。
"""

import logging

import httpx
import psycopg
import pytest

from app import auth
from app.config import Settings
from app.db import get_pool
from app.main import app
from app.sessions import SessionStore, get_session_store


@pytest.fixture
def store():
    return SessionStore()


@pytest.fixture
async def secured(pool, monkeypatch, store):
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE api_keys")
    monkeypatch.setattr("app.auth.get_settings", lambda: Settings(_env_file=None, auth_required=True))
    had_pool, old_pool = hasattr(app.state, "pool"), getattr(app.state, "pool", None)
    app.state.pool = pool
    app.dependency_overrides[get_pool] = lambda: pool
    app.dependency_overrides[get_session_store] = lambda: store
    yield pool
    app.dependency_overrides.clear()
    if had_pool:
        app.state.pool = old_pool
    else:
        del app.state.pool


@pytest.fixture
async def client(secured):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def key_for(pool, name="alice", role="operator", **kwargs) -> str:
    full_key, _ = await auth.create_key(pool, name, role, **kwargs)
    return full_key


def bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ------------------------------------------------------------ 密钥本身
def test_generated_keys_are_unique_and_parse_back():
    k1, id1, hash1 = auth.generate_key()
    k2, id2, hash2 = auth.generate_key()
    assert k1 != k2 and id1 != id2
    key_id, secret = auth.parse_key(k1)
    assert key_id == id1 and auth.hash_secret(secret) == hash1


@pytest.mark.parametrize("bad", ["", "agk_", "agk_abc", "agk_.secret", "agk_id.", "xyz_id.secret", "Bearer agk_a.b"])
def test_malformed_keys_do_not_parse(bad):
    assert auth.parse_key(bad) is None


# ------------------------------------------------------------ 401：没有身份
@pytest.mark.parametrize("path", ["/jobs", "/sessions/x"])
async def test_missing_key_is_401_with_challenge(client, path):
    response = await client.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_all_credential_failures_look_identical(client, secured):
    """缺失、乱写、不存在、被吊销、过期——响应必须完全一样，不给攻击者"这把钥匙存在过"的信息。"""
    revoked = await key_for(secured, "r")
    await auth.revoke_key(secured, auth.parse_key(revoked)[0])
    expired = await key_for(secured, "e", expires_in_days=-1)
    valid = await key_for(secured, "v")
    wrong_secret = valid[:-3] + "xxx"

    responses = [
        await client.get("/jobs", headers=h)
        for h in (
            {},
            bearer("garbage"),
            bearer("agk_deadbeef.whatever"),
            bearer(revoked),
            bearer(expired),
            bearer(wrong_secret),
        )
    ]

    assert {r.status_code for r in responses} == {401}
    assert len({r.text for r in responses}) == 1


async def test_key_in_query_string_is_rejected(client, secured):
    key = await key_for(secured)
    assert (await client.get(f"/jobs?api_key={key}")).status_code == 401


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
async def test_bearer_scheme_is_case_insensitive(client, secured, scheme):
    key = await key_for(secured)
    assert (await client.get("/jobs", headers={"Authorization": f"{scheme} {key}"})).status_code == 200


async def test_other_schemes_are_rejected(client, secured):
    key = await key_for(secured)
    assert (await client.get("/jobs", headers={"Authorization": f"Basic {key}"})).status_code == 401
    assert (await client.get("/jobs", headers={"Authorization": key})).status_code == 401


# ------------------------------------------------------------ 403：有身份但没权限
async def test_viewer_can_read_jobs_but_not_investigate(client, secured):
    key = await key_for(secured, "v", "viewer")
    assert (await client.get("/jobs", headers=bearer(key))).status_code == 200
    assert (await client.post("/sessions", json={}, headers=bearer(key))).status_code == 403
    assert (await client.post("/chat", json={"message": "hi"}, headers=bearer(key))).status_code == 403
    assert (await client.post("/investigate", json={}, headers=bearer(key))).status_code == 403


async def test_operator_can_open_sessions(client, secured):
    key = await key_for(secured, "o", "operator")
    assert (await client.post("/sessions", json={}, headers=bearer(key))).status_code == 201


async def test_public_endpoints_need_no_key(client):
    assert (await client.get("/health")).status_code == 200


# ------------------------------------------------------------ 吊销、过期、使用记录
async def test_revoked_key_stops_working_immediately(client, secured):
    key = await key_for(secured)
    assert (await client.get("/jobs", headers=bearer(key))).status_code == 200
    assert await auth.revoke_key(secured, auth.parse_key(key)[0])
    assert (await client.get("/jobs", headers=bearer(key))).status_code == 401


async def test_expired_key_is_rejected(client, secured):
    key = await key_for(secured, expires_in_days=-1)
    assert (await client.get("/jobs", headers=bearer(key))).status_code == 401


async def test_unknown_role_in_database_is_rejected(client, secured):
    key = await key_for(secured)
    async with secured.connection() as conn:
        # CHECK 约束会挡住非法角色；这里绕开它，模拟"有人手工改库"，验证代码这一层也不会放行。
        await conn.execute("ALTER TABLE api_keys DROP CONSTRAINT api_keys_role_check")
        await conn.execute("UPDATE api_keys SET role = 'superuser'")
    try:
        assert (await client.get("/jobs", headers=bearer(key))).status_code == 401
    finally:
        async with secured.connection() as conn:
            await conn.execute("TRUNCATE api_keys")
            await conn.execute(
                "ALTER TABLE api_keys ADD CONSTRAINT api_keys_role_check CHECK (role IN ('viewer','operator','admin'))"
            )


async def test_use_updates_last_used_at(client, secured):
    key = await key_for(secured)
    assert (await auth.list_keys(secured))[0]["last_used_at"] is None
    await client.get("/jobs", headers=bearer(key))
    assert (await auth.list_keys(secured))[0]["last_used_at"] is not None


# ------------------------------------------------------------ 密钥不会泄露
async def test_database_holds_only_the_hash(secured):
    key = await key_for(secured)
    _, secret = auth.parse_key(key)
    async with secured.connection() as conn:
        rows = await (await conn.execute("SELECT * FROM api_keys")).fetchall()
    flat = " ".join(str(v) for row in rows for v in row)
    assert secret not in flat and key not in flat
    assert auth.hash_secret(secret) in flat


async def test_list_keys_never_exposes_the_hash(secured):
    await key_for(secured)
    [row] = await auth.list_keys(secured)
    assert "key_hash" not in row and "secret" not in str(row)


async def test_rejected_key_is_not_logged(client, caplog):
    secret_marker = "super-secret-marker-123"
    with caplog.at_level(logging.DEBUG):
        await client.get("/jobs", headers=bearer(f"agk_deadbeef.{secret_marker}"))
    assert secret_marker not in caplog.text


async def test_active_names_are_unique_but_reusable_after_revocation(secured):
    first = await key_for(secured, "alice")
    with pytest.raises(psycopg.errors.UniqueViolation):
        await key_for(secured, "alice")
    await auth.revoke_key(secured, auth.parse_key(first)[0])
    await key_for(secured, "alice")  # 旧的已吊销，可以沿用同一个名字


# ------------------------------------------------------------ 会话所有权（IDOR）
async def make_session(client, key) -> str:
    response = await client.post("/sessions", json={}, headers=bearer(key))
    assert response.status_code == 201
    return response.json()["id"]


async def test_other_users_cannot_see_or_use_my_session(client, secured):
    alice = await key_for(secured, "alice")
    bob = await key_for(secured, "bob")
    session_id = await make_session(client, alice)

    assert (await client.get(f"/sessions/{session_id}", headers=bearer(alice))).status_code == 200
    for method, path in (
        ("GET", f"/sessions/{session_id}"),
        ("POST", f"/sessions/{session_id}/messages"),
        ("POST", f"/sessions/{session_id}/messages/stream"),
    ):
        response = await client.request(method, path, json={"message": "hi"}, headers=bearer(bob))
        # 404 而不是 403：不确认"这个会话存在"
        assert response.status_code == 404, (method, path)
        assert response.json() == {"detail": "session not found"}


async def test_not_yours_and_not_existing_look_identical(client, secured):
    alice = await key_for(secured, "alice")
    bob = await key_for(secured, "bob")
    session_id = await make_session(client, alice)

    theirs = await client.get(f"/sessions/{session_id}", headers=bearer(bob))
    missing = await client.get("/sessions/does-not-exist", headers=bearer(bob))

    assert (theirs.status_code, theirs.text) == (missing.status_code, missing.text)


async def test_admin_can_open_any_session(client, secured):
    alice = await key_for(secured, "alice")
    root = await key_for(secured, "root", "admin")
    session_id = await make_session(client, alice)
    assert (await client.get(f"/sessions/{session_id}", headers=bearer(root))).status_code == 200


async def test_session_owner_is_the_key_name(client, secured, store):
    alice = await key_for(secured, "alice")
    session_id = await make_session(client, alice)
    assert store.peek(session_id).owner == "alice"


async def test_viewer_cannot_read_sessions(client, secured):
    alice = await key_for(secured, "alice")
    viewer = await key_for(secured, "view", "viewer")
    session_id = await make_session(client, alice)
    assert (await client.get(f"/sessions/{session_id}", headers=bearer(viewer))).status_code == 403


# ------------------------------------------------------------ 故障与开关
async def test_database_down_fails_closed_with_503(client, monkeypatch):
    async def boom(pool, presented):
        raise psycopg.OperationalError("db down")

    monkeypatch.setattr(auth, "authenticate_key", boom)
    response = await client.get("/jobs", headers=bearer("agk_deadbeef.whatever"))
    assert response.status_code == 503


async def test_missing_pool_fails_closed_with_503(client, monkeypatch):
    app.state.pool = None  # fixture 会在测试结束后还原
    response = await client.get("/jobs", headers=bearer("agk_deadbeef.whatever"))
    assert response.status_code == 503


async def test_auth_disabled_lets_everything_through(pool, monkeypatch):
    monkeypatch.setattr("app.auth.get_settings", lambda: Settings(_env_file=None, auth_required=False))
    app.dependency_overrides[get_pool] = lambda: pool
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            assert (await c.get("/jobs")).status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_auth_is_required_by_default():
    # conftest 为了让其他测试不受影响设置了 AUTH_REQUIRED=false，所以这里直接检查字段的【默认值】，而不是读运行时的配置。
    assert Settings.model_fields["auth_required"].default is True


# ------------------------------------------------------------ CORS
def test_cors_preflight_allows_the_authorization_header():
    from fastapi.testclient import TestClient

    response = TestClient(app).options(
        "/sessions",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert "authorization" in response.headers["access-control-allow-headers"].lower()
