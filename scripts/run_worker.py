"""单独运行后台 Worker（不带 Web 服务）：领取并执行排队的调查任务。

    .venv/bin/python scripts/run_worker.py

默认情况下 Worker 和 Web 服务在同一个进程里（见 app/main.py 的 lifespan）。生产环境更常见的做法是拆开：
  Web 服务    WORKER_ENABLED=false，只处理 HTTP 请求，可以按请求量扩缩容，甚至缩到 0
  Worker 进程  运行这个脚本，按队列长度扩缩容；一次调查要几十秒到几分钟，不能被 Web 的请求超时打断
为什么要拆：Cloud Run 这类平台只在"有请求"的时候才给服务分配 CPU，没有请求时后台协程会被冻结，
同进程的 Worker 就不干活了。拆开之后，Worker 是独立运行的容器，不受请求影响。
两边共享同一个数据库里的任务队列（FOR UPDATE SKIP LOCKED），所以可以随意起多个 Worker，不会重复领取。

收到 SIGTERM（容器停止、Cloud Run 缩容、Ctrl-C）时优雅退出：做完手头这个任务（最多等 SHUTDOWN_GRACE_SECONDS），
做不完就把它放回队列，不丢任务。
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path

# 直接运行脚本时，Python 的模块搜索路径里是脚本所在的目录（scripts/）而不是项目根目录。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.heartbeat import heartbeat_path, run_heartbeat  # noqa: E402
from app.db import create_pool, init_knowledge_schema, init_schema  # noqa: E402
from app.llm.factory import LLMNotConfigured, create_llm_client  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.rag.embedder import create_embedder  # noqa: E402
from app.rag.retrieval import KnowledgeBase  # noqa: E402
from app.telemetry import configure_telemetry  # noqa: E402
from app.worker import Worker  # noqa: E402

logger = logging.getLogger("run_worker")


async def run() -> int:
    settings = Settings()
    configure_logging(settings.log_level)
    try:
        llm = create_llm_client(settings)
    except LLMNotConfigured as exc:
        # 没有模型就没法调查：与其启动后让每个任务都失败、耗尽重试次数，不如直接不启动（任务留在队列里等配置好）。
        print(f"Cannot start the worker: {exc}", file=sys.stderr)
        return 2

    telemetry = configure_telemetry(settings.otel_exporter, settings.otel_endpoint, settings.otel_sample_ratio, settings.environment)
    pool = create_pool(settings.database_url, settings.db_pool_max_size, settings.db_pool_timeout_seconds)
    await pool.open(wait=False)
    try:
        await init_schema(pool)  # 有咨询锁保护，和 Web 实例同时启动也安全
        knowledge = None
        if await init_knowledge_schema(pool):
            try:
                knowledge = KnowledgeBase(pool, create_embedder(settings))
            except LLMNotConfigured:
                logger.warning("knowledge search disabled: embedder is not configured")

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        worker = Worker(pool, llm, settings, knowledge=knowledge)
        # 心跳：容器的健康检查靠它判断 Worker 是不是还活着（见 app/heartbeat.py）。
        beat_task = asyncio.create_task(run_heartbeat(heartbeat_path(), stop), name="heartbeat")
        task = asyncio.create_task(worker.run(stop), name="investigation-worker")
        await stop.wait()  # 一直等到收到停止信号
        logger.info("stop signal received, finishing the current job")
        try:
            await asyncio.wait_for(task, settings.shutdown_grace_seconds)
        except (TimeoutError, asyncio.CancelledError):
            logger.warning("worker did not finish in time; its job was released back to the queue")
        await beat_task  # stop 已经设置，心跳任务会自己结束
        return 0
    finally:
        # 顺序和 Web 服务一样：先停 Worker（上面已经停了），再冲刷监控数据，最后才关连接池。
        telemetry.shutdown()
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
