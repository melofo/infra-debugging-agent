"use client";
// 自动调查列表页：显示 Jira 新建 Bug 后自动触发的调查，每 3 秒刷新一次，所以能看到
// 任务从 Queued -> Running -> Done 的变化。

import Link from "next/link";
import { useCallback } from "react";
import { StatusBadge } from "@/components/StatusBadge";
import { usePolling } from "@/hooks/usePolling";
import { ApiError, listJobs } from "@/lib/api";
import { timeAgo } from "@/lib/format";

const REFRESH_MS = 3000;

export default function JobsPage() {
  // useCallback 让 fetcher 在多次渲染之间保持同一个引用。
  // usePolling 的 effect 依赖它：如果每次渲染都是新函数，effect 会不停地重启，轮询就乱了。
  const fetchJobs = useCallback((signal: AbortSignal) => listJobs(signal), []);
  const { data: jobs, error, loading } = usePolling(fetchJobs, REFRESH_MS);

  return (
    <main className="page-content">
      <h1>Automatic investigations</h1>
      <p className="subtitle">
        Started automatically when Jira reports a new Bug. This page refreshes every {REFRESH_MS / 1000} seconds.
      </p>

      {error && <div className="error">{describe(error)}</div>}
      {loading && !jobs && <p className="subtitle">Loading…</p>}

      {jobs && jobs.length === 0 && (
        <div className="empty">
          <p>No automatic investigations yet.</p>
          <p className="hint">They appear here when Jira sends a &quot;Bug created&quot; webhook.</p>
        </div>
      )}

      {jobs && jobs.length > 0 && (
        <table className="jobs">
          <thead>
            <tr>
              <th>Ticket</th>
              <th>Status</th>
              <th>Most likely cause</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => (
              <tr key={job.id}>
                <td>
                  <Link href={`/jobs/${job.id}`}>
                    <strong>{job.ticket_id}</strong>
                  </Link>
                </td>
                <td>
                  <StatusBadge status={job.status} />
                  {job.attempts > 1 && job.status !== "succeeded" && (
                    <span className="hint"> attempt {job.attempts}/{job.max_attempts}</span>
                  )}
                </td>
                <td className="cause">
                  {job.top_cause ? (
                    <>
                      {job.top_cause}
                      {job.confidence !== null && <span className="hint"> ({Math.round(job.confidence * 100)}%)</span>}
                    </>
                  ) : job.last_error ? (
                    <span className="hint">{job.last_error}</span>
                  ) : (
                    <span className="hint">—</span>
                  )}
                </td>
                <td className="hint">{timeAgo(job.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}

function describe(error: Error): string {
  if (error instanceof ApiError && error.status === 503) {
    return "The database is unavailable. Start it with `docker compose up -d db`; this page will recover by itself.";
  }
  return "Could not load investigations. Is the backend running?";
}
