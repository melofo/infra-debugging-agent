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

from app import telemetry
from app.agent.common import AGENT_VARIANTS, AgentError, StepRecord, session_system_prompt
from app.auth import Principal, require_scope
from app.agent.loop import InvestigationAgent
from app.agent.tools import build_investigation_tools
from app.api.chat import get_llm_client
from app.config import get_settings
from app.llm.base import LLMClient, LLMError
from app.rag.runtime import active_knowledge_base
from app.scenario import load_scenario
from app.sessions import (
    Session,
    SessionConflict,
    SessionStorage,
    SessionTooLarge,
    SessionUnavailable,
    Turn,
    get_session_store,
    release_shielded,
)

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
    request: CreateSessionRequest,
    http_request: Request,
    store: SessionStorage = Depends(get_session_store),
    principal: Principal = Depends(require_scope("investigate:run")),
) -> SessionOut:
    try:
        scenario = load_scenario(request.scenario)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"scenario not found: {request.scenario}")
    # 会话创建时决定用不用知识库检索；整个会话期间工具集保持不变，对话历史里的工具调用才前后一致。
    knowledge = await active_knowledge_base(getattr(http_request.app.state, "knowledge", None))
    # 新会话用当前配置的 agent 版本，并记下来：以后恢复会话、每一轮对话都按创建时的版本，工具集不会中途变化。
    variant = get_settings().agent_variant
    registry = build_investigation_tools(scenario, knowledge, service_discovery=AGENT_VARIANTS[variant].service_discovery)
    session = await store.create(
        request.scenario, scenario.ticket.id, registry, owner=principal.name,
        uses_knowledge=knowledge is not None, agent_variant=variant,
    )
    return _to_out(session)


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session(
    session_id: str,
    store: SessionStorage = Depends(get_session_store),
    principal: Principal = Depends(require_scope("investigate:run")),
) -> SessionOut:
    return _to_out(await _find_session(store, session_id, principal))


@router.post("/sessions/{session_id}/messages", response_model=Turn)
async def post_message(
    session_id: str,
    body: MessageIn,
    store: SessionStorage = Depends(get_session_store),
    llm: LLMClient = Depends(get_llm_client),
    principal: Principal = Depends(require_scope("investigate:run")),
) -> Turn:
    session = await _find_session(store, session_id, principal)
    await _acquire(store, session)
    try:
        return await _run_session_turn(store, session, body.message, llm)
    finally:
        # 无论成功失败都必须释放，否则这个会话会一直显示"正忙"（数据库版的租约到期后也会自动恢复，但不该靠它）。
        # 用 release_shielded：请求被取消（客户端断开）时，普通的 await 会立刻再次被取消，释放就发不出去。
        await release_shielded(store, session)


@router.post("/sessions/{session_id}/messages/stream")
async def post_message_stream(
    session_id: str,
    body: MessageIn,
    store: SessionStorage = Depends(get_session_store),
    llm: LLMClient = Depends(get_llm_client),
    principal: Principal = Depends(require_scope("investigate:run")),
) -> StreamingResponse:
    session = await _find_session(store, session_id, principal)
    # 注意：404/409 这类错误必须在开始推流【之前】返回。
    # 一旦开始发送 SSE，HTTP 状态码已经是 200 发出去了，之后再出错只能通过事件告诉客户端。
    # 这里只做"是否正忙"的提前检查，真正拿锁放在下面的生成器里。
    # 为什么？如果在这里拿锁、在生成器的 finally 里释放，那么客户端在"返回响应"和
    # "生成器开始执行"之间断开的话，生成器从未启动，finally 永远不会运行，
    # 锁就泄漏了，这个会话会被永久锁死。把"拿锁"和"释放"放进同一个 try/finally 才安全。
    if await store.is_busy(session):
        raise HTTPException(status_code=409, detail="session is busy, wait for the previous message")

    async def event_stream() -> AsyncIterator[str]:
        # 上面的检查和这里拿锁之间隔了一段时间，期间可能被别的请求抢先，所以要再检查一次：
        # 真正的"占用"是 acquire 这一步（原子操作），上面只是为了能在推流开始前就返回真实的 409。
        if not await store.acquire(session):
            error = json.dumps({"status": 409, "detail": "session is busy, wait for the previous message"})
            yield f"event: error\ndata: {error}\n\n"
            return

        # 生产者-消费者模式：agent 在后台任务里跑，把事件放进队列；这个生成器从队列取出来推给浏览器。
        # 为什么不直接在生成器里跑 agent？因为 agent 的回调（on_step）需要把事件"推出去"，
        # 而生成器只能在自己的 yield 处产出，用队列把两边解耦最简单。
        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()

        async def on_step(record: StepRecord) -> None:
            await queue.put(("step", json.dumps(dataclasses.asdict(record), ensure_ascii=False)))

        async def runner() -> None:
            try:
                turn = await _run_session_turn(store, session, body.message, llm, on_step)
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
            await release_shielded(store, session)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        # no-cache：别缓存事件流。X-Accel-Buffering：告诉 nginx 之类的代理不要攒着再发，要实时转发。
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _find_session(store: SessionStorage, session_id: str, principal: Principal) -> Session:
    try:
        session = await store.get(session_id)
    except SessionUnavailable:
        logger.warning("session cannot be restored", extra={"session_id": session_id})
        raise HTTPException(status_code=503, detail="this session cannot be restored right now")
    # 不是所有者（也不是管理员）：返回 404，而不是 403。
    # 403 会告诉对方"这个会话存在，只是你没权限"，等于确认了会话 id 是有效的；404 让"不存在"和"不属于你"无法区分。
    if session is None or (session.owner != principal.name and not principal.is_admin):
        raise HTTPException(status_code=404, detail="session not found")
    return session


async def _acquire(store: SessionStorage, session: Session) -> None:
    """尝试占用会话；正忙就直接报 409，不排队（排队会让请求越堆越多）。"""
    try:
        acquired = await store.acquire(session)
    except SessionUnavailable:
        raise HTTPException(status_code=503, detail="this session cannot be restored right now")
    if not acquired:
        raise HTTPException(status_code=409, detail="session is busy, wait for the previous message")


async def _run_session_turn(
    store: SessionStorage,
    session: Session,
    message: str,
    llm: LLMClient,
    on_step: Callable[[StepRecord], Awaitable[None]] | None = None,
) -> Turn:
    """在会话里执行一轮。调用方必须已经占用了会话（acquire）。普通接口和流式接口共用这一份逻辑。"""
    settings = get_settings()
    if len(session.turns) >= settings.max_turns_per_session:
        # 轮数上限同时限制了历史增长：历史越长，每一轮请求的 token 越多、越贵。
        raise HTTPException(status_code=409, detail="session reached its turn limit, start a new session")

    agent = InvestigationAgent(
        llm,
        session.registry,
        max_steps=settings.agent_max_steps,
        system_prompt=session_system_prompt(session.ticket_id, session.agent_variant),
    )
    # 会话 id 是"知道它就能访问会话"的凭证，不能原样写进追踪（有追踪权限的人就都能访问会话了）：只记哈希前缀。
    with telemetry.span(
        "session.turn", **{"session.id_hash": telemetry.opaque_id(session.id), "session.turn_index": len(session.turns) + 1}
    ):
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
    try:
        await store.save(session)  # 一轮完整成功之后才保存：失败的轮次不会留下半截历史（run_turn 已经回滚了内存里的）
    except SessionTooLarge:
        raise HTTPException(status_code=409, detail="this session is too large, start a new session")
    except SessionConflict:
        logger.warning("session changed while a turn was running", extra={"session_id": session.id})
        raise HTTPException(status_code=409, detail="the session was changed elsewhere; reload it and try again")
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
