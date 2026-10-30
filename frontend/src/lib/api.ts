/**
 * 调用后端 API 的封装。界面组件不直接写 fetch，都通过这里，
 * 这样后端地址、错误处理、SSE 解析只需要维护一处（类似 Java 里的 Service / Client 层）。
 */
import { authHeaders } from "./auth";
import { parseSse } from "./sse";
import type { JobDetail, JobSummary, SessionInfo, Step, Turn } from "./types";

// NEXT_PUBLIC_ 前缀的环境变量会在【构建时】被写死进发给浏览器的 JS 里，浏览器才读得到。
// 没有这个前缀的变量只在服务端可见，浏览器里是 undefined。
// 所以这里只能放"可以公开"的内容（后端地址），绝不能放密钥。
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** 后端出错时返回 {"detail": "..."}；把它取出来变成可读的错误。 */
async function toApiError(response: Response): Promise<ApiError> {
  let detail = response.statusText;
  try {
    const body = await response.json();
    if (typeof body.detail === "string") detail = body.detail;
  } catch {
    // 响应体不是 JSON，就用状态文字。
  }
  // 401 / 403 对用户来说是"该怎么办"的问题，给出可操作的提示，而不是原样显示服务端的一句话。
  if (response.status === 401) detail = "Missing or invalid API key. Enter a valid key in the top bar.";
  if (response.status === 403) detail = "This API key is not allowed to do that. Ask for a key with more access.";
  return new ApiError(response.status, detail);
}

export async function createSession(scenario = "bug-1042"): Promise<SessionInfo> {
  const response = await fetch(`${API_URL}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ scenario }),
  });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

export interface StreamHandlers {
  onStep: (step: Step) => void;
  onTurn: (turn: Turn) => void;
}

/**
 * 发送一条消息，并实时接收 Agent 的进度。
 *
 * 流程：POST -> 逐个读取 SSE 事件 -> step 事件调用 onStep，turn 事件调用 onTurn。
 * signal 是"取消信号"：用户离开页面或开始新会话时可以中止请求（对应后端的"客户端断开"处理）。
 */
export async function sendMessage(
  sessionId: string,
  message: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_URL}/sessions/${encodeURIComponent(sessionId)}/messages/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ message }),
    signal,
  });
  // 推流开始前的错误（404 会话不存在、409 正忙）是普通的 HTTP 错误响应。
  if (!response.ok) throw await toApiError(response);
  if (!response.body) throw new ApiError(0, "response has no body");

  let gotTurn = false;
  for await (const { event, data } of parseSse(response.body)) {
    if (event === "step") {
      handlers.onStep(JSON.parse(data) as Step);
    } else if (event === "turn") {
      gotTurn = true;
      handlers.onTurn(JSON.parse(data) as Turn);
    } else if (event === "error") {
      // 推流开始后发生的错误：HTTP 状态已经是 200 了，所以错误放在事件里。
      const { status, detail } = JSON.parse(data) as { status: number; detail: string };
      throw new ApiError(status, detail);
    }
  }
  // 流正常结束却没有收到最终结果，说明连接被中途切断了。
  if (!gotTurn) throw new ApiError(0, "connection closed before the answer was complete");
}

/** 最近的自动调查任务（列表页）。signal 用来在组件卸载时取消请求。 */
export async function listJobs(signal?: AbortSignal): Promise<JobSummary[]> {
  const response = await fetch(`${API_URL}/jobs`, { headers: authHeaders(), signal });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}

/** 单个任务的完整详情。 */
export async function getJob(id: string, signal?: AbortSignal): Promise<JobDetail> {
  const response = await fetch(`${API_URL}/jobs/${encodeURIComponent(id)}`, { headers: authHeaders(), signal });
  if (!response.ok) throw await toApiError(response);
  return response.json();
}
