"""应用入口：创建 FastAPI 应用并把各个路由挂上去。

启动命令 `uvicorn app.main:app --reload` 的含义：
- uvicorn：Web 服务器（Java 里相当于 Tomcat/Netty）。FastAPI 只定义接口，监听端口的是 uvicorn。
- app.main:app：去 app/main.py 这个模块里找名叫 app 的对象（即文件最底部那个变量）。
- --reload：代码改动后自动重启，仅开发用。
"""

from fastapi import FastAPI

from app.api import chat, health, investigate
from app.config import get_settings
from app.logging_config import configure_logging


def create_app() -> FastAPI:
    # "应用工厂" 模式：把创建过程放进函数，测试时可以创建干净的实例。
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title=settings.app_name)
    # Router 相当于 Spring 的 @RestController：每个文件负责一组接口，这里统一注册。
    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(investigate.router)
    return app


# 模块级变量，就是 uvicorn 命令里 `app.main:app` 指向的对象。
app = create_app()
