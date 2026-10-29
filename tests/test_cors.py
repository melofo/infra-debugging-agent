"""CORS 测试：只有白名单里的网页来源才被允许跨域调用 API。

浏览器发真正的请求之前，会先发一个 OPTIONS "预检请求"问服务端：
"来自这个来源的网页，可以用这个方法、带这些请求头来调你吗？"
服务端在响应头里回答，浏览器据此决定放不放行。我们在这里直接模拟这个预检请求。
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def preflight(origin: str):
    return client.options(
        "/sessions",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )


def test_local_frontend_origin_is_allowed():
    response = preflight("http://localhost:3000")
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_unknown_origin_is_not_allowed():
    response = preflight("https://evil.example.com")
    # 没有 allow-origin 头，浏览器就会拦截这个跨域请求。
    assert "access-control-allow-origin" not in response.headers


def test_simple_cross_origin_response_carries_allow_header():
    response = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
