# pytest 约定：文件名以 test_ 开头、函数名以 test_ 开头，pytest 就会自动发现并运行。
# 不需要像 JUnit 那样写类和 @Test 注解；断言直接用 Python 的 assert。
from fastapi.testclient import TestClient

from app.main import app

# TestClient 在进程内直接调用 FastAPI 应用，不需要真的启动服务器、占用端口。
client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "infra-debugging-agent"
