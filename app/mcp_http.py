"""把 MCP 服务端通过 HTTP（Streamable HTTP）暴露给网络上的其他程序，并强制 Bearer 密钥认证。

    客户端 --HTTPS--> [认证中间件：校验密钥 + 权限] --> MCP 协议处理 --> 调查工具

【为什么 HTTP 版本必须有认证，而 stdio 版本不需要】
stdio：服务端是调用方自己启动的子进程，只有它的父进程能读写那根管道。"能启动它"本身就是授权。
HTTP：任何能连上这个端口的人都能发请求，必须自己验明身份。没有认证的 MCP 端口 = 任何人都能读你的日志和工单。

【和 REST 接口共用同一套认证】
中间件调用的就是 app/auth.py 里的 authenticate_request（REST 接口用的也是它）：
同样的密钥、同样的角色、同样的"统一 401"、同样的数据库故障时拒绝（503）。MCP 工具要求 investigate:run 权限。

【几个有意的选择】
  stateless_http + json_response：这个服务端只提供"调用工具"，不需要服务端主动推送，所以不保留会话状态。
      每个请求自带全部信息，任何一个实例都能处理，水平扩展（比如 Cloud Run 起多个实例）不需要会话粘滞。
  请求体上限很小：工具参数只有几百字节，4MB 的默认值只会给攻击者留空间。
  DNS rebinding 防护：恶意网页可以让浏览器把请求发到 127.0.0.1 上你本机的服务。校验 Host/Origin 请求头能挡住它。
      SDK 只会在绑定回环地址时自动开启；绑定到其他地址时，我们要求显式给出允许的 Host，否则拒绝启动。
"""

import logging

from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from psycopg_pool import AsyncConnectionPool
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth import FORBIDDEN_DETAIL, AuthFailure, authenticate_request
from app.config import LOOPBACK_HOSTS

logger = logging.getLogger(__name__)

MCP_REQUIRED_SCOPE = "investigate:run"
MAX_BODY_BYTES = 64 * 1024


class McpAuthMiddleware:
    """纯 ASGI 中间件：在任何 MCP 处理发生之前先验明身份。

    Java 类比：Servlet Filter / Spring Security 的过滤器链最前面的一环。
    """

    def __init__(self, app: ASGIApp, pool: AsyncConnectionPool | None, required_scope: str = MCP_REQUIRED_SCOPE):
        self.app = app
        self.pool = pool
        self.required_scope = required_scope

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # lifespan（启动/关闭）等事件原样放行，否则服务起不来
            await self.app(scope, receive, send)
            return
        try:
            principal = await authenticate_request(self.pool, Headers(scope=scope).get("authorization"))
            if not principal.has(self.required_scope):
                logger.warning("forbidden", extra={"actor": principal.name, "scope": self.required_scope})
                raise AuthFailure(403, FORBIDDEN_DETAIL)
        except AuthFailure as failure:
            # 注意：任何路径、任何方法，没有有效密钥一律先挡在这里，连"这个路径存不存在"都不告诉陌生人。
            response = JSONResponse({"detail": failure.detail}, status_code=failure.status, headers=failure.headers)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_mcp_http_app(
    server: Server,
    pool: AsyncConnectionPool | None,
    *,
    host: str = "127.0.0.1",
    allowed_hosts: list[str] | None = None,
    path: str = "/mcp",
) -> Starlette:
    if pool is None:
        # 没有数据库就没法校验密钥：宁可不启动，也不要启动一个不设防的端口。
        raise ValueError("an MCP HTTP server needs a database pool to check API keys")

    security = None  # 回环地址：SDK 自动开启 DNS rebinding 防护
    if host not in LOOPBACK_HOSTS:
        if not allowed_hosts:
            raise ValueError(f"binding to {host!r} requires explicit allowed hosts (DNS rebinding protection)")
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=list(allowed_hosts))

    app = server.streamable_http_app(
        streamable_http_path=path,
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_BODY_BYTES,
        transport_security=security,
        host=host,
    )
    app.add_middleware(McpAuthMiddleware, pool=pool)
    return app
