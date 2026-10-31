"use client";
// 审批页：Agent 提议的写操作（比如往 Jira 回写评论）都在这里，必须由有权限的人看过内容后批准才会执行。

import { useCallback, useState } from "react";
import { ApprovalCard } from "@/components/ApprovalCard";
import { usePolling } from "@/hooks/usePolling";
import { ApiError, listActions } from "@/lib/api";

const REFRESH_MS = 5000;

export default function ApprovalsPage() {
  // refreshKey 变化 -> fetcher 变成新函数 -> usePolling 重新开始并立刻请求一次：用来在审批之后马上刷新。
  const [refreshKey, setRefreshKey] = useState(0);
  const fetchActions = useCallback(
    (signal: AbortSignal) => {
      void refreshKey;
      return listActions(undefined, signal);
    },
    [refreshKey],
  );
  const { data: actions, error, loading } = usePolling(fetchActions, REFRESH_MS);

  const pending = actions?.filter((a) => a.status === "pending" && !a.expired) ?? [];
  const history = actions?.filter((a) => !(a.status === "pending" && !a.expired)) ?? [];

  return (
    <main className="page-content">
      <h1>Approvals</h1>
      <p className="subtitle">
        After an automatic investigation finishes, the agent proposes a Jira comment. It is only posted after a person
        with approval rights reviews the exact text and approves it.
      </p>

      {error && <div className="error">{describe(error)}</div>}
      {loading && !actions && <p className="subtitle">Loading…</p>}
      {actions && pending.length === 0 && (
        <div className="empty">
          <p>Nothing is waiting for approval.</p>
        </div>
      )}

      {pending.map((a) => (
        <ApprovalCard key={a.id} action={a} onChanged={() => setRefreshKey((k) => k + 1)} />
      ))}

      {history.length > 0 && <h2>History</h2>}
      {history.map((a) => (
        <ApprovalCard key={a.id} action={a} onChanged={() => setRefreshKey((k) => k + 1)} />
      ))}
    </main>
  );
}

function describe(error: Error): string {
  if (error instanceof ApiError && (error.status === 401 || error.status === 403)) return error.message;
  if (error instanceof ApiError && error.status === 503) return "The database is unavailable.";
  return "Could not load approvals. Is the backend running?";
}
