from fastapi import APIRouter

from app.config import get_settings

router = APIRouter()


# @router.get("/health") 是"装饰器"（decorator），相当于 Java 的 @GetMapping("/health")：
# 声明这个函数处理 GET /health 请求。FastAPI 会读取这个声明，自动生成 /docs 页面里的条目。
# 返回类型标注 dict[str, str] 会进入 OpenAPI 文档；返回的 dict 会自动序列化成 JSON。
@router.get("/health")
def health() -> dict[str, str]:
    # 健康检查接口：Cloud Run / 负载均衡器会定期访问它，判断服务是否活着。
    settings = get_settings()
    return {"status": "ok", "app": settings.app_name, "environment": settings.environment}
