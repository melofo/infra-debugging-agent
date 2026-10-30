from fastapi import APIRouter, HTTPException, Request

from app.config import get_settings
from app.db import ping

router = APIRouter()


# @router.get("/health") 是"装饰器"（decorator），相当于 Java 的 @GetMapping("/health")：
# 声明这个函数处理 GET /health 请求。FastAPI 会读取这个声明，自动生成 /docs 页面里的条目。
# 返回类型标注 dict[str, str] 会进入 OpenAPI 文档；返回的 dict 会自动序列化成 JSON。
@router.get("/health")
def health() -> dict[str, str]:
    # 存活检查（liveness）：只回答"这个进程还活着吗"。Cloud Run / 负载均衡器会定期访问它。
    # 注意它【不检查数据库】：数据库暂时连不上时，重启应用也解决不了问题，
    # 如果因此判定应用"已死"而反复重启它，反而会让故障更严重。
    settings = get_settings()
    return {"status": "ok", "app": settings.app_name, "environment": settings.environment}


@router.get("/ready")
async def ready(request: Request) -> dict[str, str]:
    # 就绪检查（readiness）：回答"这个实例现在能正常处理请求吗"，会检查依赖（数据库）。
    # 返回 503 的实例，负载均衡器会暂时不给它派流量，但不会杀掉它。
    # 存活检查和就绪检查的区别，是 Kubernetes / Cloud Run 里的标准概念，面试常问。
    pool = getattr(request.app.state, "pool", None)
    if pool is None or not await ping(pool):
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ready"}
