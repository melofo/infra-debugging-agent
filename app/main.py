"""应用入口：创建 FastAPI 应用并把各个路由挂上去。

启动命令 `uvicorn app.main:app --reload` 的含义：
- uvicorn：Web 服务器（Java 里相当于 Tomcat/Netty）。FastAPI 只定义接口，监听端口的是 uvicorn。
- app.main:app：去 app/main.py 这个模块里找名叫 app 的对象（即文件最底部那个变量）。
- --reload：代码改动后自动重启，仅开发用。
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import chat, health, investigate, jobs, sessions, webhooks
from app.config import get_settings
from app.db import create_pool, init_knowledge_schema, init_schema
from app.llm.factory import LLMNotConfigured, create_llm_client
from app.logging_config import configure_logging
from app.worker import Worker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用的启动和关闭流程。yield 之前是启动，之后是关闭。

    相当于 Spring 里的 @PostConstruct / @PreDestroy，或者 ApplicationListener<ContextRefreshedEvent>。
    """
    settings = get_settings()
    pool = create_pool(settings.database_url, settings.db_pool_max_size, settings.db_pool_timeout_seconds)
    app.state.pool = pool

    # open(wait=False)：在后台建立连接，不阻塞启动，也不会因为数据库没开就启动失败。
    # 这是"降级运行"的设计：数据库不可用时，聊天和调查功能照常工作，
    # 只有依赖数据库的功能（Webhook、任务队列）返回 503。/ready 接口会如实反映数据库状态。
    await pool.open(wait=False)
    try:
        await init_schema(pool)
        logger.info("database ready")
        # 知识库表需要 pgvector：失败只意味着语义检索不可用，不影响其他功能。
        app.state.knowledge_ready = await init_knowledge_schema(pool)
    except Exception:
        logger.exception("database not available at startup; job features disabled until restart")

    # 启动后台 Worker（和 Web 服务放在同一个进程里，作为一个后台协程）。
    # 需要模型可用才有意义：没配置 API key 时不启动，任务会留在队列里，配置好重启后继续处理。
    worker_task = None
    stop = asyncio.Event()
    if settings.worker_enabled:
        try:
            worker = Worker(pool, create_llm_client(settings), settings)
            worker_task = asyncio.create_task(worker.run(stop), name="investigation-worker")
        except LLMNotConfigured:
            logger.warning("worker not started: LLM is not configured; queued jobs will wait")
    app.state.worker_task = worker_task

    yield

    # 【优雅关闭】顺序很重要：先停 worker，再关连接池。
    # 如果先关了连接池，正在执行的任务连"放回队列"都做不到，会留下一个永远显示"执行中"的任务。
    if worker_task is not None:
        stop.set()  # 通知 worker："做完手头这个就停"
        try:
            # 最多等 shutdown_grace_seconds 让当前任务做完；超时 wait_for 会取消 worker，
            # worker 在被取消时会把当前任务放回队列（见 Worker.process）。
            await asyncio.wait_for(worker_task, settings.shutdown_grace_seconds)
        except (TimeoutError, asyncio.CancelledError):
            logger.warning("worker did not finish in time; its job was released back to the queue")
        except Exception:
            logger.exception("worker crashed during shutdown")

    await pool.close()
    # 关闭之后不要留着一个已关闭的池：之后任何人再取它都会拿到一个"死对象"。
    # 置为 None，依赖它的代码（get_pool）会如实报告"数据库不可用"（503）。
    app.state.pool = None


def create_app() -> FastAPI:
    # "应用工厂" 模式：把创建过程放进函数，测试时可以创建干净的实例。
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    # 【CORS：跨域资源共享】
    # 浏览器有"同源策略"：网页（localhost:3000）默认不允许去调用另一个来源（localhost:8000）的接口，
    # 哪怕只是端口不同。这是为了防止恶意网页借用你的登录状态偷偷访问别的网站。
    # 解除限制要由【服务端】明确声明"我允许哪些来源"：浏览器先发一个 OPTIONS 预检请求问一下，
    # 服务端回答允许，才会发真正的请求。
    # 注意：CORS 只约束浏览器，curl 和别的服务器不受影响，所以它【不是】认证手段。
    # 这里用白名单而不是 "*"（允许所有来源）；以后加了登录，allow_origins 为 * 时浏览器也不会带凭证。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    # Router 相当于 Spring 的 @RestController：每个文件负责一组接口，这里统一注册。
    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(investigate.router)
    app.include_router(sessions.router)
    app.include_router(webhooks.router)
    app.include_router(jobs.router)
    return app


# 模块级变量，就是 uvicorn 命令里 `app.main:app` 指向的对象。
app = create_app()
