/**
 * 把时间和耗时格式化成人容易读的样子。纯函数（相同输入永远得到相同输出，没有副作用），
 * 所以很容易写单元测试：把"当前时间"作为参数传进来，而不是在函数里偷偷读系统时钟。
 */

/** "3 min ago" 这种相对时间。now 作为参数传入，测试时可以固定。 */
export function timeAgo(iso: string, now: number = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.floor(hours / 24)} d ago`;
}

/** 毫秒 -> "4.1s"。 */
export function formatDuration(ms: number): string {
  return `${(ms / 1000).toFixed(1)}s`;
}

/** 完整的 UTC 时间，用于悬停提示：2026-10-20 14:02:11 UTC。 */
export function formatUtc(iso: string): string {
  return `${new Date(iso).toISOString().replace("T", " ").slice(0, 19)} UTC`;
}
