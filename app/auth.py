"""接口认证与授权：API key + 角色。

【认证和授权是两回事】
  认证 (authentication)  你是谁？  -> 校验 API key，得到身份（Principal）
  授权 (authorization)   你能做什么？ -> 看身份的角色有没有这个操作所需的权限（scope）
前者失败返回 401（"我不知道你是谁"），后者失败返回 403（"我知道你是谁，但你不被允许"）。

【密钥的格式与存储】
    agk_<key_id>.<secret>
    key_id  公开的查找标识（存明文），用来在数据库里找到这把钥匙对应的行
    secret  真正的机密，256 位随机数。数据库里只存它的 SHA-256 哈希
为什么不用 bcrypt 之类的慢哈希？那是为"人类自己选的低熵密码"设计的：故意算得很慢，让暴力猜测不划算。
我们的 secret 是 256 位的随机数，暴力破解本来就不可行；用慢哈希只会拖慢每一个请求。

【角色和权限】最小权限原则：每个角色只拿它需要的。
    viewer    只能查看调查结果
    operator  还能发起调查、使用聊天
    admin     还能做审批等管理操作（Phase 6.6）
"""

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi import Depends, HTTPException, Request
from psycopg_pool import AsyncConnectionPool

from app.config import get_settings

logger = logging.getLogger(__name__)

KEY_PREFIX = "agk_"

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "viewer": frozenset({"jobs:read"}),
    "operator": frozenset({"jobs:read", "investigate:run"}),
    "admin": frozenset({"jobs:read", "investigate:run", "approvals:decide"}),
}


@dataclass(frozen=True)
class Principal:
    """"是谁在调用"。会出现在日志、审计记录、会话所有者里。"""

    key_id: str
    name: str
    role: str

    def has(self, scope: str) -> bool:
        return scope in ROLE_SCOPES.get(self.role, frozenset())

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# 关闭认证时（仅本地开发和自动化测试）使用的身份。
DEV_PRINCIPAL = Principal(key_id="dev", name="anonymous-dev", role="admin")


# ---------------------------------------------------------------------------
# 密钥的生成、解析、哈希
# ---------------------------------------------------------------------------
def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_key() -> tuple[str, str, str]:
    """返回 (完整密钥, key_id, secret 的哈希)。完整密钥只在创建时出现这一次。"""
    key_id = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)  # 256 位随机数。必须用 secrets，不能用 random：random 的输出是可预测的
    return f"{KEY_PREFIX}{key_id}.{secret}", key_id, hash_secret(secret)


def parse_key(value: str) -> tuple[str, str] | None:
    """把 `agk_<id>.<secret>` 拆开；格式不对返回 None。"""
    if not value.startswith(KEY_PREFIX) or "." not in value:
        return None
    key_id, _, secret = value[len(KEY_PREFIX) :].partition(".")
    return (key_id, secret) if key_id and secret else None


# ---------------------------------------------------------------------------
# 密钥管理（供 scripts/manage_api_keys.py 使用，不暴露成 HTTP 接口以减少攻击面）
# ---------------------------------------------------------------------------
async def create_key(
    pool: AsyncConnectionPool, name: str, role: str, expires_in_days: float | None = None
) -> tuple[str, str]:
    if role not in ROLE_SCOPES:
        raise ValueError(f"unknown role {role!r}; choose from {sorted(ROLE_SCOPES)}")
    full_key, key_id, key_hash = generate_key()
    expires_at = datetime.now(timezone.utc) + timedelta(days=expires_in_days) if expires_in_days else None
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO api_keys (key_id, name, role, key_hash, expires_at) VALUES (%s, %s, %s, %s, %s)",
            (key_id, name, role, key_hash, expires_at),
        )
    return full_key, key_id


async def list_keys(pool: AsyncConnectionPool) -> list[dict]:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT key_id, name, role, created_at, expires_at, revoked_at, last_used_at "
            "FROM api_keys ORDER BY created_at"  # 注意：绝不选出 key_hash
        )
        columns = [c.name for c in cursor.description]
        return [dict(zip(columns, row)) for row in await cursor.fetchall()]


async def revoke_key(pool: AsyncConnectionPool, key_id: str) -> bool:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE key_id = %s AND revoked_at IS NULL", (key_id,)
        )
        return cursor.rowcount == 1


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
_DUMMY_HASH = hash_secret("dummy-secret-used-to-equalise-timing")


async def authenticate_key(pool: AsyncConnectionPool, presented: str) -> Principal | None:
    """校验一把 API key。通过返回身份，任何原因失败都返回 None（调用方不区分原因）。"""
    parsed = parse_key(presented)
    if parsed is None:
        return None
    key_id, secret = parsed

    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT name, role, key_hash, expires_at, revoked_at FROM api_keys WHERE key_id = %s", (key_id,)
            )
        ).fetchone()

        # 找不到这个 key_id 时，也照样做一次哈希比较，让"存在"和"不存在"的耗时尽量一样，不给攻击者可测量的差别。
        stored_hash = row[2] if row else _DUMMY_HASH
        # 常量时间比较（和 Webhook 验签一样）：== 遇到第一个不同字符就返回，耗时泄露了"前面几位是对的"。
        matches = hmac.compare_digest(hash_secret(secret), stored_hash)
        if row is None or not matches:
            return None

        name, role, _, expires_at, revoked_at = row
        if revoked_at is not None:
            return None
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            return None
        if role not in ROLE_SCOPES:  # 数据库里被人手工改成了未知角色：宁可拒绝，也不要猜测它的权限
            return None

        await conn.execute("UPDATE api_keys SET last_used_at = now() WHERE key_id = %s", (key_id,))
    return Principal(key_id=key_id, name=name, role=role)


def bearer_token(authorization: str | None) -> str | None:
    scheme, _, token = (authorization or "").partition(" ")
    # 只接受 Authorization 请求头里的 Bearer 令牌（方案名按规范不区分大小写）。
    # 故意不支持 URL 里的 ?api_key=...：URL 会被写进反向代理、浏览器历史、访问日志，等于把密钥贴在了公告栏上。
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


class AuthFailure(Exception):
    """认证失败。和具体的 Web 框架无关：REST 接口把它转成 HTTPException，MCP 的 ASGI 中间件把它转成 JSON 响应。

    把校验逻辑只写在一处（authenticate_request），两个入口共用，规则就不会各改各的、慢慢走样。
    """

    def __init__(self, status: int, detail: str, headers: dict[str, str] | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.headers = headers or {}


def _unauthorized() -> AuthFailure:
    # 所有失败原因（缺失、格式不对、不存在、已吊销、已过期）返回完全一样的响应：
    # 不告诉攻击者"这把钥匙存在，只是被吊销了"。
    return AuthFailure(401, "invalid or missing API key", {"WWW-Authenticate": "Bearer"})


async def authenticate_request(pool: AsyncConnectionPool | None, authorization: str | None) -> Principal:
    """从 Authorization 请求头的值得到调用者身份；失败抛 AuthFailure。"""
    token = bearer_token(authorization)
    if token is None:
        raise _unauthorized()
    if pool is None:
        raise AuthFailure(503, "authentication is temporarily unavailable")
    try:
        principal = await authenticate_key(pool, token)
    except psycopg.OperationalError:
        # 数据库不可用时【拒绝】，而不是放行："fail closed"。代价是数据库挂了的时候受保护的接口也不可用，这是安全优先的取舍。
        logger.exception("authentication lookup failed")
        raise AuthFailure(503, "authentication is temporarily unavailable") from None
    if principal is None:
        logger.warning("rejected an API key")  # 不记录密钥本身
        raise _unauthorized()
    return principal


FORBIDDEN_DETAIL = "this API key is not allowed to do that"


async def current_principal(request: Request) -> Principal:
    if not get_settings().auth_required:
        return DEV_PRINCIPAL
    try:
        return await authenticate_request(
            getattr(request.app.state, "pool", None), request.headers.get("authorization")
        )
    except AuthFailure as failure:
        raise HTTPException(status_code=failure.status, detail=failure.detail, headers=failure.headers or None)


def require_scope(scope: str):
    """生成一个 FastAPI 依赖：要求调用者有指定权限，返回他的身份。"""

    async def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.has(scope):
            logger.warning("forbidden", extra={"actor": principal.name, "scope": scope})
            raise HTTPException(status_code=403, detail=FORBIDDEN_DETAIL)
        return principal

    return dependency
