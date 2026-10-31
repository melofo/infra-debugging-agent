"use client";
/**
 * 一条待审批的写操作。核心是让审批人看清【将要发出去的确切内容】，批准的就是这一份。
 *
 * 安全相关的几个细节：
 * - 内容用 <pre> 当纯文本显示，不当 HTML 渲染（React 默认就会转义文本，所以评论里即使有 <script> 也不会执行）；
 * - 批准时把我们【显示的那份内容的哈希】发回去，不是让服务端再去取"最新的"：
 *   如果显示之后内容变过，服务端会拒绝，审批人要重新看一遍；
 * - 按钮在请求进行中禁用，避免重复点击（服务端也有原子保护，这里只是不让用户困惑）。
 */
import { useState } from "react";
import { ApiError, approveAction, rejectAction } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import type { ActionItem } from "@/lib/types";

export function ApprovalCard({ action, onChanged }: { action: ActionItem; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");

  const decidable = action.status === "pending" && !action.expired;

  async function run(call: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await call();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not reach the backend.");
    } finally {
      setBusy(false);
      onChanged(); // 不管成功失败都刷新列表：内容变了、已被别人处理等情况，让界面显示最新状态
    }
  }

  return (
    <section className="action-card">
      <header className="action-head">
        <strong>Post a comment on {action.ticket_id}</strong>
        <span className={`action-status action-${action.expired ? "expired" : action.status}`}>
          {action.expired ? "expired" : action.status}
        </span>
      </header>
      <p className="hint">
        Proposed by {action.proposed_by} {timeAgo(action.created_at)}. Nothing is posted until a person approves it.
      </p>
      <pre className="action-body">{action.body}</pre>
      <p className="hint">Content fingerprint: {action.content_hash.slice(0, 12)}</p>

      {decidable && (
        <div className="action-controls">
          <input
            value={note}
            maxLength={500}
            placeholder="Optional note"
            onChange={(e) => setNote(e.target.value)}
            disabled={busy}
          />
          <button disabled={busy} onClick={() => run(() => approveAction(action.id, action.content_hash, note))}>
            Approve and post
          </button>
          <button disabled={busy} className="secondary" onClick={() => run(() => rejectAction(action.id, note))}>
            Reject
          </button>
        </div>
      )}
      {action.decided_by && (
        <p className="hint">
          {action.status === "rejected" ? "Rejected" : "Approved"} by {action.decided_by}
          {action.decision_note ? `: ${action.decision_note}` : ""}
        </p>
      )}
      {action.status === "failed" && <div className="error">Posting failed: {action.error}</div>}
      {error && <div className="error">{error}</div>}
    </section>
  );
}
