"""会话接口：创建会话、发送消息（首次调查 / 追问）、查看历史。

  POST /sessions                         新建会话，返回 session id
  POST /sessions/{id}/messages           发一条消息，等整轮结束后一次返回 JSON
  POST /sessions/{id}/messages/stream    发一条消息，边执行边实时推送进度（SSE）
  GET  /sessions/{id}                    取回整个会话（刷新页面时恢复界面）

REST 的读法：URL 是"资源"，HTTP 方法是"动作"。
POST /sessions/{id}/messages = 在"这个会话的消息列表"里新增一条。

【SSE（Server-Sent Events）是什么】
普通 HTTP：请求 -> 等 -> 一次性返回完整响应。
SSE：请求之后，服务端【保持连接不关】，想推数据就推一段，浏览器实时收到。
它只是一种文本格式，每个事件长这样（空行结束一个事件）：

    event: step
    data: {"tool": "search_logs", ...}

    event: turn
    data: {...}

和 WebSocket 相比：SSE 只能服务端 -> 客户端单向推，但足够我们用（客户端的输入走普通 POST），
而且就是普通 HTTP，不需要特殊协议，代理和负载均衡器更容易支持。
"""

import asyncio
import contextlib
import dataclasses
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.common import AgentError, StepRecord, session_system_prompt
from app.agent.loop import InvestigationAgent
from app.agent.tools import build_investigation_tools
from app.api.chat import get_llm_client
from app.config import get_settings
from app.llm.base import LLMClient, LLMError
from app.rag.runtime import active_knowledge_base
from app.scenario import load_scenario
from app.sessions import Session, SessionStore, Turn, get_session_store

router = APIRouter()
logger = logging.getLogger(__name__)


class CreateSessionRequest(BaseModel):
    scenario: str = Field(default="bug-1042", pattern=r"^[a-z0-9-]+$", max_length=64)


class SessionOut(BaseModel):
    id: str
    scenario: str
    ticket_id: str
    turns: list[Turn]
    total_input_tokens: int
    total_output_tokens: int


class MessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


@router.post("/sessions", response_model=SessionOut, status_code=201)  # 201 = Created，创建资源的标准状态码
async def create_session(
    request: CreateSessionRequest, http_request: Request, store: SessionStore = Depends(get_session_store)
) -> SessionOut:
    try:
        scenario = load_scenario(request.scenario)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"scenario not found: {request.scenario}")
    # 会话创建时决定用不用知识库检索；整个会话期间工具集保持不变，对话历史里的工具调用才前后一致。
    knowledge = await active_knowledge_base(getattr(http_request.app.state, "knowledge", None))
    registry = build_investigation_tools(scenario, knowledge)
    session = store.create(request.scenario, scenario.ticket.id, registry)
    return _to_out(session)


@router.get("/sessions/{session_id}", response_model=SessionOut)
def get_session(session_id: str, store: SessionStore = Depends(get_session_store)) -> SessionOut:
    return _to_out(_find_session(store, session_id))


@router.post("/sessions/{session_id}/messages", response_model=Turn)
async def post_message(
    session_id: str,
    body: MessageIn,
    store: SessionStore = Depends(get_session_store),
    llm: LLMClient = Depends(get_llm_client),
) -> Turn:
    session = _find_session(store, session_id)
    await _acquire(session)
    try:
        return await _run_session_turn(session, body.message, llm)
    finally:
        session.lock.release()  # 无论成功失败都必须释放锁，否则这个会话会被永久锁死


@router.post("/sessions/{session_id}/messages/stream")
async def post_message_stream(
    session_id: str,
    body: MessageIn,
    store: SessionStore = Depends(get_session_store),
    llm: LLMClient = Depends(get_llm_client),
) -> StreamingResponse:
    session = _find_session(store, session_id)
    # 注意：404/409 这类错误必须在开始推流【之前】返回。
    # 一旦开始发送 SSE，HTTP 状态码已经是 200 发出去了，之后再出错只能通过事件告诉客户端。
    # 这里只做"是否正忙"的提前检查，真正拿锁放在下面的生成器里。
    # 为什么？如果在这里拿锁、在生成器的 finally 里释放，那么客户端在"返回响应"和
    # "生成器开始执行"之间断开的话，生成器从未启动，finally 永远不会运行，
    # 锁就泄漏了，这个会话会被永久锁死。把"拿锁"和"释放"放进同一个 try/finally 才安全。
    if session.lock.locked():
        raise HTTPException(status_code=409, detail="session is busy, wait for the previous message")

    async def event_stream() -> AsyncIterator[str]:
        # 上面的检查和这里拿锁之间隔了一段时间，期间可能被别的请求抢先，所以要再检查一次。
        # 下面两行之间没有 await，所以是原子的（asyncio.Lock 空闲时 acquire 不会让出控制权）。
        if session.lock.locked():
            error = json.dumps({"status": 409, "detail": "session is busy, wait for the previous message"})
            yield f"event: error\ndata: {error}\n\n"
            return
        await session.lock.acquire()

        # 生产者-消费者模式：agent 在后台任务里跑，把事件放进队列；这个生成器从队列取出来推给浏览器。
        # 为什么不直接在生成器里跑 agent？因为 agent 的回调（on_step）需要把事件"推出去"，
        # 而生成器只能在自己的 yield 处产出，用队列把两边解耦最简单。
        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()

        async def on_step(record: StepRecord) -> None:
            await queue.put(("step", json.dumps(dataclasses.asdict(record), ensure_ascii=False)))

        async def runner() -> None:
            try:
                turn = await _run_session_turn(session, body.message, llm, on_step)
                await queue.put(("turn", turn.model_dump_json()))
            except HTTPException as exc:
                # 推流已经开始，状态码改不了了，把错误作为一个事件发给客户端。
                await queue.put(("error", json.dumps({"status": exc.status_code, "detail": exc.detail})))
            finally:
                await queue.put(None)  # 哨兵值：告诉生成器"没有更多事件了"

        task = None
        try:
            task = asyncio.create_task(runner())
            while (item := await queue.get()) is not None:
                event, data = item
                yield f"event: {event}\ndata: {data}\n\n"  # SSE 的文本格式，空行结束一个事件
        finally:
            # 正常结束，或者客户端中途断开（生成器被取消），都会走到这里。
            # 断开时：取消后台任务，让 run_turn 里的回滚逻辑运行，把残缺的历史撤销掉。
            # 必须【等】任务真正结束之后再释放锁，否则别的请求可能在回滚完成前就开始，读到脏历史。
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            session.lock.release()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        # no-cache：别缓存事件流。X-Accel-Buffering：告诉 nginx 之类的代理不要攒着再发，要实时转发。
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _find_session(store: SessionStore, session_id: str) -> Session:
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


async def _acquire(session: Session) -> None:
    """尝试拿到会话锁；拿不到（正忙）就直接报 409，不排队。"""
    # 检查"是否正忙"和下面的"获取锁"之间没有 await，asyncio 是单线程协作式调度，
    # 所以这两步之间不会被别的请求插队，不会出现"都以为自己拿到了锁"。
    # 选择直接返回 409（冲突）而不是排队等待：避免请求越堆越多，也让前端明确知道要等一下。
    if session.lock.locked():
        raise HTTPException(status_code=409, detail="session is busy, wait for the previous message")
    await session.lock.acquire()


async def _run_session_turn(
    session: Session,
    message: str,
    llm: LLMClient,
    on_step: Callable[[StepRecord], Awaitable[None]] | None = None,
) -> Turn:
    """在会话里执行一轮。调用方必须已经持有 session.lock。普通接口和流式接口共用这一份逻辑。"""
    settings = get_settings()
    if len(session.turns) >= settings.max_turns_per_session:
        # 轮数上限同时限制了历史增长：历史越长，每一轮请求的 token 越多、越贵。
        raise HTTPException(status_code=409, detail="session reached its turn limit, start a new session")

    agent = InvestigationAgent(
        llm,
        session.registry,
        max_steps=settings.agent_max_steps,
        system_prompt=session_system_prompt(session.ticket_id),
    )
    try:
        # 还没有报告 = 这是首次调查，必须产出报告；已有报告 = 追问，文字回答即可。
        result = await agent.run_turn(
            session.messages,
            message,
            session.ticket_id,
            require_report=session.report is None,
            on_step=on_step,
        )
    except LLMError:
        logger.exception("llm call failed in session")
        raise HTTPException(status_code=502, detail="LLM provider error")
    except AgentError as exc:
        logger.error("turn failed", extra={"reason": str(exc)})
        raise HTTPException(status_code=500, detail="agent did not complete this message")

    turn = Turn(
        user_message=message,
        answer=result.answer,
        report=result.report,
        steps=result.steps,
        llm_calls=result.llm_calls,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=result.latency_ms,
    )
    session.turns.append(turn)
    if result.report is not None:
        session.report = result.report
    logger.info(
        "session turn done",
        extra={
            "turn": len(session.turns),
            "history_messages": len(session.messages),
            "turn_input_tokens": turn.input_tokens,
            "session_input_tokens": session.total_input_tokens,
        },
    )
    return turn


def _to_out(session: Session) -> SessionOut:
    return SessionOut(
        id=session.id,
        scenario=session.scenario_name,
        ticket_id=session.ticket_id,
        turns=session.turns,
        total_input_tokens=session.total_input_tokens,
        total_output_tokens=session.total_output_tokens,
    )
