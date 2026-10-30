"use client";
// 上面这行 "use client" 把本文件标记为 Client Component（在浏览器里运行）。
// Next.js 里页面默认是 Server Component（在服务端渲染，没有状态、不能响应点击）。
// 聊天界面需要状态（useState）、事件处理（onSubmit）和浏览器 API，所以必须是 Client Component。

import { useEffect, useRef, useState } from "react";
import { ReportCard } from "@/components/ReportCard";
import { StepList } from "@/components/StepList";
import { ApiError, createSession, sendMessage } from "@/lib/api";
import type { SessionInfo, Step, Turn } from "@/lib/types";

export default function Home() {
  // useState 返回 [当前值, 修改函数]。调用修改函数会让组件【重新渲染】，界面自动刷新。
  // 可以把它理解成"带自动刷新功能的成员变量"。
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [pending, setPending] = useState<string | null>(null); // 正在处理的用户消息
  const [liveSteps, setLiveSteps] = useState<Step[]>([]); // 正在处理的这一轮里，已完成的步骤
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);

  const busy = pending !== null;
  // useRef 保存"不需要触发重新渲染"的值。这里存的是用来中止请求的控制器。
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  // useEffect：在渲染完成之后执行的副作用。依赖数组里的值变化时才重新执行。
  // 这里的作用是：有新内容时自动滚动到底部。
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, liveSteps, pending]);

  // 组件卸载（离开页面）时，中止还在进行的请求。后端会据此取消任务并回滚历史。
  useEffect(() => () => abortRef.current?.abort(), []);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault(); // 阻止表单默认的"刷新整个页面"行为
    const message = input.trim();
    if (!message || busy) return;

    setInput("");
    setError(null);
    setPending(message);
    setLiveSteps([]);

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      // 第一次发消息时才创建会话（懒创建）。
      const current = session ?? (await createSession());
      if (!session) setSession(current);

      await sendMessage(
        current.id,
        message,
        {
          // 注意用函数形式 setX(prev => ...)：基于"最新的值"更新，
          // 而不是基于渲染时捕获的旧值。事件密集到达时，这样才不会丢数据。
          onStep: (step) => setLiveSteps((prev) => [...prev, step]),
          onTurn: (turn) => {
            setTurns((prev) => [...prev, turn]);
            setLiveSteps([]);
          },
        },
        controller.signal,
      );
    } catch (e) {
      if (controller.signal.aborted) return; // 是我们主动取消的，不算错误
      // 404 = 服务端不认识这个会话（会话存在服务器内存里，服务重启就没了）。
      // 既然知道它已经失效，就丢掉它：否则之后每条消息都会拿着这个死 id 再 404 一次，
      // 用户得手动点 New session 才能恢复。丢掉之后，下一条消息会自动创建新会话。
      if (e instanceof ApiError && e.status === 404) setSession(null);
      setError(e instanceof ApiError ? describeError(e) : "Could not reach the server. Is the backend running?");
    } finally {
      setPending(null);
    }
  }

  function handleNewSession() {
    abortRef.current?.abort();
    setSession(null);
    setTurns([]);
    setLiveSteps([]);
    setPending(null);
    setError(null);
  }

  const totalIn = turns.reduce((sum, t) => sum + t.input_tokens, 0);
  const totalOut = turns.reduce((sum, t) => sum + t.output_tokens, 0);

  return (
    <main className="app">
      <header className="topbar">
        <div>
          <h1>Chat</h1>
          <p className="subtitle">
            {session ? `Session on ${session.ticket_id} (simulated incident)` : "Ask about the simulated incident BUG-1042"}
          </p>
        </div>
        <button type="button" className="secondary" onClick={handleNewSession} disabled={turns.length === 0 && !busy}>
          New session
        </button>
      </header>

      <div className="messages">
        {turns.length === 0 && !busy && (
          <div className="empty">
            <p>Try:</p>
            <button type="button" className="suggestion" onClick={() => setInput("Investigate BUG-1042")}>
              Investigate BUG-1042
            </button>
            <p className="hint">The first message runs a full investigation. Later messages are follow-up questions.</p>
          </div>
        )}

        {turns.map((turn, index) => (
          <TurnView key={index} turn={turn} />
        ))}

        {pending && (
          <>
            <div className="bubble user">{pending}</div>
            <StepList steps={liveSteps} running />
          </>
        )}

        {error && <div className="error">{error}</div>}
        <div ref={bottomRef} />
      </div>

      {turns.length > 0 && (
        <p className="usage">
          Session total: {totalIn.toLocaleString()} input / {totalOut.toLocaleString()} output tokens
        </p>
      )}

      <form className="composer" onSubmit={handleSubmit}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={turns.length === 0 ? "Describe what to investigate…" : "Ask a follow-up question…"}
          maxLength={2000}
          disabled={busy}
          autoFocus
        />
        <button type="submit" disabled={busy || input.trim() === ""}>
          {busy ? "Working…" : "Send"}
        </button>
      </form>
    </main>
  );
}

function TurnView({ turn }: { turn: Turn }) {
  return (
    <>
      <div className="bubble user">{turn.user_message}</div>
      <StepList steps={turn.steps} />
      <div className="bubble agent">{turn.answer}</div>
      {turn.report && <ReportCard report={turn.report} />}
      <p className="usage">
        {turn.llm_calls} model call{turn.llm_calls === 1 ? "" : "s"} · {turn.input_tokens.toLocaleString()} in /{" "}
        {turn.output_tokens.toLocaleString()} out tokens · {(turn.latency_ms / 1000).toFixed(1)}s
      </p>
    </>
  );
}

/** 把后端的错误码翻译成用户看得懂的话。 */
function describeError(error: ApiError): string {
  switch (error.status) {
    case 409:
      return "The session is busy or reached its turn limit. Wait a moment, or start a new session.";
    case 404:
      return "This session no longer exists (the server probably restarted). Your next message starts a new one; the answers above stay on screen, but the agent no longer remembers them.";
    case 502:
      return "The language model service had a problem (it may be rate limited). Try again in a moment.";
    default:
      return error.message || "Something went wrong.";
  }
}
