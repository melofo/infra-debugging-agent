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

export type EvidenceSource = "jira" | "log" | "metric" | "trace" | "git" | "runbook";

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
