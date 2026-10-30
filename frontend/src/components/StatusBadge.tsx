import type { JobStatus } from "@/lib/types";

const LABELS: Record<JobStatus, string> = {
  queued: "Queued",
  running: "Running",
  succeeded: "Done",
  failed: "Failed",
};

/** 任务状态的彩色小标签。Record<JobStatus, string> 保证每种状态都有文案：漏了一个，编译就会报错。 */
export function StatusBadge({ status }: { status: JobStatus }) {
  return (
    <span className={`status status-${status}`}>
      {status === "running" && <span className="spinner" aria-hidden />}
      {LABELS[status]}
    </span>
  );
}
