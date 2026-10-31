/**
 * 和后端数据结构一一对应的 TypeScript 类型（对应后端的 app/schemas.py 和 app/sessions.py）。
 *
 * TypeScript 的 interface 只在【编译期】检查，运行时不存在：
 * 浏览器收到的只是普通 JSON，类型标注只是让编辑器和编译器帮我们发现拼写错误。
 * （Java 里的 DTO 类在运行时是真实存在的对象，这是和 Java 的一个区别。）
 *
 * 目前这些类型是手写的，后端改了字段需要手动同步。更稳妥的做法是从后端的 /openapi.json
 * 自动生成（比如 openapi-typescript 工具）；等接口稳定后可以换成那样。
 */

export type EvidenceSource = "jira" | "log" | "metric" | "trace" | "git" | "runbook" | "incident";

export interface Evidence {
  source: EvidenceSource;
  summary: string;
  reference: string;
  observed_at: string | null; // "string | null" 是联合类型，表示"字符串或 null"
}

export interface Hypothesis {
  root_cause: string;
  confidence: number; // 0 到 1
  evidence: Evidence[];
  suggested_next_steps: string[];
}

export interface RCAReport {
  incident_id: string;
  summary: string;
  hypotheses: Hypothesis[]; // 后端已按置信度从高到低排好序
  created_at: string;
}

/** Agent 做的一步：调用了某个工具。 */
export interface Step {
  step: number;
  tool: string;
  args: Record<string, unknown>; // 键是字符串、值任意类型的对象，类似 Java 的 Map<String, Object>
  ok: boolean;
  result_preview: string;
}

/** 一轮对话：用户一句话 + Agent 的完整回复。 */
export interface Turn {
  user_message: string;
  answer: string;
  report: RCAReport | null;
  steps: Step[];
  llm_calls: number;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
}

export interface SessionInfo {
  id: string;
  scenario: string;
  ticket_id: string;
  turns: Turn[];
  total_input_tokens: number;
  total_output_tokens: number;
}

// ---------------------------------------------------------------------------
// 自动调查任务（Jira Webhook 触发的），对应后端 app/api/jobs.py
// ---------------------------------------------------------------------------

export type JobStatus = "queued" | "running" | "succeeded" | "failed";

/** 列表页用的精简信息。 */
export interface JobSummary {
  id: string;
  ticket_id: string;
  status: JobStatus;
  attempts: number;
  max_attempts: number;
  last_error: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  top_cause: string | null; // 排名第一的根因（仅成功的任务有）
  confidence: number | null;
}

export interface JobResult {
  answer: string;
  report: RCAReport;
  steps: Step[];
  llm_calls: number;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
}

/** 详情页用：在 JobSummary 的基础上多了完整结果。`extends` 和 Java 里的继承一样。 */
export interface JobDetail extends JobSummary {
  scenario: string;
  run_after: string;
  result: JobResult | null;
}

/** 任务是否已经结束（不会再变化）。 */
export function isFinished(status: JobStatus): boolean {
  return status === "succeeded" || status === "failed";
}

/** 待审批的写操作（对应后端 app/actions.py 的 Action）。 */
export type ActionStatus = "pending" | "approved" | "rejected" | "executing" | "executed" | "failed";

export interface ActionItem {
  id: string;
  job_id: string;
  kind: string;
  ticket_id: string;
  body: string; // 将要发出去的【确切内容】：审批人看的就是它
  content_hash: string; // 批准时要把它原样带回去，证明"我批准的就是我看到的这一份"
  status: ActionStatus;
  proposed_by: string;
  created_at: string;
  expires_at: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  executed_at: string | null;
  error: string | null;
  expired: boolean; // 还是 pending，但已经过期
}
