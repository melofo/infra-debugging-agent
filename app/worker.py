"""后台 Worker：不断从队列领取调查任务并执行。

它做的"调查"和网页聊天里的是同一个 Agent（InvestigationAgent），区别只是：
没有人在等，所以不需要实时推送，结果直接存进任务表的 result 字段，事后查看。

【Worker 是一个长期运行的循环】
    循环：
      定期回收"卡死"的任务
      领取一个任务 → 没有就睡一会儿
      执行 → 成功就保存结果；失败就记录原因并安排重试

【几条"后台程序"的基本功，每一条都对应一个真实的故障场景】
  1. 永远不能静默死掉：循环里任何异常都要捕获、记录，然后继续。否则一个意外就让整个后台停摆，
     而且没有任何人发现（HTTP 接口出错用户会看到，后台出错没人看到）。
  2. 数据库暂时连不上：等一会儿再试，不要崩溃，也不要疯狂重试把日志刷爆。
  3. 单个任务不能无限占用：有超时。超时必须小于租约，否则会被误回收。
  4. 被要求停止时要体面：当前任务做完，或者放回队列，不能丢，也不能把"已领取"的状态留在库里。
  5. 同一时间只处理一个任务：模型有每分钟请求数限制，并发处理反而更容易被限流。
     需要更高吞吐量时，运行更多 worker 进程即可（SKIP LOCKED 保证它们不会抢到同一个任务）。
"""

import asyncio
import contextlib
import dataclasses
import logging
import os
import socket
import time
import uuid

import psycopg
from psycopg_pool import AsyncConnectionPool

from app import jobs
from app.agent.common import AgentError, InvestigationResult
from app.agent.loop import InvestigationAgent
from app.agent.tools import build_investigation_tools
from app.config import Settings
from app.jobs import Job
from app.llm.base import LLMClient, LLMError
from app.scenario import load_scenario

logger = logging.getLogger(__name__)


class InvalidJob(Exception):
    """任务本身有问题（引用的数据不存在或已损坏），重试也没用。

    消息是写给【用户看】的：它会被存进 last_error，通过 API 返回、显示在页面上。
    所以只能包含对用户有意义、对攻击者没用的信息，绝不能带内部路径、堆栈、SQL 之类的细节。
    （细节只写进服务端日志，见 process。）
    """


def make_worker_id() -> str:
    """每个 worker 的唯一标识，写进任务的 locked_by，排查问题时能看出是谁领走的。"""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def result_to_dict(result: InvestigationResult) -> dict:
    """把调查结果转成能存进 jsonb 的字典。mode="json" 会把 datetime 等转成字符串。"""
    assert result.report is not None
    return {
        "answer": result.answer,
        "report": result.report.model_dump(mode="json"),
        "steps": [dataclasses.asdict(step) for step in result.steps],
        "llm_calls": result.llm_calls,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "latency_ms": result.latency_ms,
    }


class Worker:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        llm: LLMClient,
        settings: Settings,
        worker_id: str | None = None,
    ):
        self._pool = pool
        self._llm = llm
        self._settings = settings
        self.worker_id = worker_id or make_worker_id()
        self._last_reclaim = float("-inf")  # 单调时钟读数；-inf 表示"从没回收过"，第一轮就会检查

    # ------------------------------------------------------------------ 主循环
    async def run(self, stop: asyncio.Event) -> None:
        """循环直到 stop 被设置。stop 是一个"事件"，类似 Java 里的 volatile boolean + 唤醒机制。"""
        logger.info("worker started", extra={"worker_id": self.worker_id})
        while not stop.is_set():
            try:
                worked = await self.run_once()
            except asyncio.CancelledError:
                raise  # 取消要继续往上传，不能吞掉，否则关闭流程会卡住
            except psycopg.OperationalError:
                # 数据库暂时不可用：不崩溃，等一会儿再试。
                logger.warning("worker cannot reach the database, will retry", exc_info=True)
                worked = False
            except Exception:
                # 兜底：任何没预料到的异常都不能让 worker 静默死掉。
                logger.exception("unexpected error in worker loop")
                worked = False
            if not worked:
                # 队列空了：睡一会儿。用 stop.wait 来睡而不是 sleep，这样收到停止信号时能立刻醒来。
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self._settings.worker_poll_interval_seconds)
        logger.info("worker stopped", extra={"worker_id": self.worker_id})

    async def run_once(self) -> bool:
        """做一轮：必要时回收卡死任务，领取并执行至多一个任务。返回有没有执行任务。

        把"一轮"单独拆成方法，测试时可以精确控制每一步，不用等待真实的时间流逝。
        """
        await self._maybe_reclaim()
        job = await jobs.claim(self._pool, self.worker_id)
        if job is None:
            return False
        await self.process(job)
        return True

    async def _maybe_reclaim(self) -> None:
        now = time.monotonic()  # 单调时钟：不受系统时间被调整的影响，适合算时间间隔
        if now - self._last_reclaim < self._settings.reclaim_interval_seconds:
            return
        self._last_reclaim = now
        reclaimed = await jobs.reclaim_stuck(self._pool, self._settings.job_lease_seconds)
        if reclaimed:
            logger.warning("reclaimed stuck jobs", extra={"count": reclaimed})

    # ------------------------------------------------------------------ 执行一个任务
    async def process(self, job: Job) -> None:
        log = {"job_id": str(job.id), "ticket_id": job.ticket_id, "attempt": job.attempts}
        logger.info("job started", extra=log)
        try:
            # 超时保护：一次调查最多 job_timeout_seconds。asyncio.timeout 是 Python 3.11 的写法，
            # 超时会抛 TimeoutError，并取消里面正在进行的调用。
            async with asyncio.timeout(self._settings.job_timeout_seconds):
                result = await self._investigate(job)
        except asyncio.CancelledError:
            # 服务正在关闭，worker 被取消：把任务原样放回队列，不扣尝试次数，也不留下"执行中"的假象。
            await jobs.release(self._pool, job.id, self.worker_id, job.attempts)
            logger.info("job released back to the queue", extra=log)
            raise
        except InvalidJob as exc:
            # 任务引用的数据不存在或已损坏。这种问题重试一百次也一样，直接最终失败，别浪费钱。
            await self._fail(job, f"invalid job: {exc}", permanent=True)
        except TimeoutError:
            await self._fail(job, f"investigation timed out after {self._settings.job_timeout_seconds:g}s")
        except (LLMError, AgentError) as exc:
            # 模型服务出问题（限流、过载）或 Agent 没能完成：可能是暂时的，按退避重试。
            # 截断长度：服务商的错误信息可能很长，只留开头足够排查，完整内容在日志里。
            logger.warning("investigation failed", extra={**log, "error": str(exc)})
            await self._fail(job, f"{type(exc).__name__}: {str(exc)[:200]}")
        except Exception as exc:
            # 意料之外的异常，它的消息可能含内部细节（路径、SQL、连接串）。
            # 完整信息和堆栈只写日志，存进数据库、对外展示的只有异常的类型名。
            logger.exception("unexpected error while processing job", extra=log)
            await self._fail(job, f"unexpected {type(exc).__name__} (see server logs)")
        else:
            done = await jobs.complete(self._pool, job.id, self.worker_id, job.attempts, result_to_dict(result))
            if done is None:
                # 围栏拒绝了我们：任务在这期间被回收并交给了别人。我们的结果作废，不能覆盖别人的。
                logger.warning("job was reclaimed while running; result discarded", extra=log)
            else:
                logger.info("job succeeded", extra={**log, "llm_calls": result.llm_calls})

    async def _investigate(self, job: Job) -> InvestigationResult:
        # 只在"加载数据"这一步把这两类错误认定为"任务本身有问题"。
        # 范围一定要窄：如果把整个调查过程里的 ValueError 都当成永久失败，
        # 我们自己代码里的 bug 就会被误判成"数据坏了"，任务不会重试，问题还被掩盖了。
        try:
            scenario = load_scenario(job.scenario)
        except FileNotFoundError:
            raise InvalidJob("no investigation data for this ticket") from None
        except ValueError:
            logger.exception("scenario data is invalid", extra={"scenario": job.scenario})
            raise InvalidJob("investigation data for this ticket is invalid") from None
        registry = build_investigation_tools(scenario)
        agent = InvestigationAgent(self._llm, registry, max_steps=self._settings.agent_max_steps)
        return await agent.investigate(job.ticket_id)

    async def _fail(self, job: Job, error: str, permanent: bool = False) -> None:
        failed = await jobs.fail(
            self._pool,
            job.id,
            self.worker_id,
            job.attempts,
            error,
            base_delay_seconds=self._settings.job_retry_base_delay_seconds,
            max_delay_seconds=self._settings.job_retry_max_delay_seconds,
            permanent=permanent,
        )
        if failed is None:
            logger.warning("job was reclaimed while running; failure ignored", extra={"job_id": str(job.id)})
            return
        # 重试时记 WARNING，最终失败记 ERROR（ERROR 才应该触发告警，需要人介入）。
        level = logging.ERROR if failed.status == "failed" else logging.WARNING
        logger.log(
            level,
            "job failed" if failed.status == "failed" else "job failed, will retry",
            extra={"job_id": str(job.id), "attempt": job.attempts, "status": failed.status, "error": error},
        )
