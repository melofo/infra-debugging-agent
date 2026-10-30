"use client";
// 单个自动调查任务的详情：状态、失败原因、完整的 RCA 报告和调查步骤。
// 任务还没结束时每 3 秒刷新；一旦结束（成功或失败）就停止轮询，不再打扰后端。

import Link from "next/link";
import { useCallback } from "react";
import { ReportCard } from "@/components/ReportCard";
import { StatusBadge } from "@/components/StatusBadge";
import { StepList } from "@/components/StepList";
import { usePolling } from "@/hooks/usePolling";
import { ApiError, getJob } from "@/lib/api";
import { formatDuration, formatUtc, timeAgo } from "@/lib/format";
import { isFinished, type JobDetail } from "@/lib/types";

// 模块级的函数，引用永远不变，可以放心传给 usePolling。
const jobIsFinished = (job: JobDetail) => isFinished(job.status);

export function JobDetailView({ jobId }: { jobId: string }) {
  const fetchJob = useCallback((signal: AbortSignal) => getJob(jobId, signal), [jobId]);
  // 任务还在排队或执行时每 3 秒刷新；一旦成功或失败就停止轮询，不再打扰后端。
  const { data: job, error, loading } = usePolling(fetchJob, 3000, jobIsFinished);

  return (
    <main className="page-content">
      <p>
        <Link href="/jobs">← All investigations</Link>
      </p>

      {error && error instanceof ApiError && error.status === 404 && <div className="error">Investigation not found.</div>}
      {error && !(error instanceof ApiError && error.status === 404) && (
        <div className="error">Could not load this investigation. Is the backend running?</div>
      )}
      {loading && !job && <p className="subtitle">Loading…</p>}

      {job && (
        <>
          <header className="job-header">
            <h1>{job.ticket_id}</h1>
            <StatusBadge status={job.status} />
          </header>
          <p className="subtitle" title={formatUtc(job.created_at)}>
            Created {timeAgo(job.created_at)} · attempt {job.attempts} of {job.max_attempts}
          </p>

          {job.status === "queued" && (
            <div className="notice">
              {job.attempts === 0
                ? "Waiting for a worker to pick this up."
                : `A previous attempt failed (${job.last_error ?? "unknown error"}). Next attempt: ${formatUtc(job.run_after)}.`}
            </div>
          )}
          {job.status === "running" && <div className="notice">Investigating… the report will appear here.</div>}
          {job.status === "failed" && (
            <div className="error">
              <strong>Investigation failed.</strong> {job.last_error}
            </div>
          )}

          {job.result && (
            <>
              <div className="bubble agent">{job.result.answer}</div>
              <ReportCard report={job.result.report} />
              <h3 className="section-title">How the agent investigated</h3>
              <StepList steps={job.result.steps} />
              <p className="usage">
                {job.result.llm_calls} model calls · {job.result.input_tokens.toLocaleString()} in /{" "}
                {job.result.output_tokens.toLocaleString()} out tokens · {formatDuration(job.result.latency_ms)}
              </p>
            </>
          )}
        </>
      )}
    </main>
  );
}
