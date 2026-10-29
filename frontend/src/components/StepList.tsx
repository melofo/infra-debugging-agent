/**
 * 展示 Agent 的调查过程：它依次调用了哪些工具、参数是什么、返回了什么。
 *
 * 这个组件没有 "use client"，也没有状态，只是"数据进、界面出"的纯函数。
 * React 组件就是返回界面描述（JSX）的函数；props 是传给它的参数（类似 Java 方法的入参）。
 */
import type { Step } from "@/lib/types";

export function StepList({ steps, running = false }: { steps: Step[]; running?: boolean }) {
  if (steps.length === 0 && !running) return null;
  return (
    <ul className="steps">
      {steps.map((step, index) => (
        // React 渲染列表时，每一项需要一个稳定的 key，用来判断哪些项变了、哪些可以复用。
        <li key={`${step.step}-${step.tool}-${index}`} className={step.ok ? "step" : "step step-failed"}>
          <details>
            <summary>
              <span className="step-mark">{step.ok ? "✓" : "✗"}</span>
              <code>{step.tool}</code>
              <span className="step-args">{formatArgs(step.args)}</span>
            </summary>
            <pre className="step-result">{step.result_preview}</pre>
          </details>
        </li>
      ))}
      {running && (
        <li className="step step-running">
          <span className="spinner" aria-hidden /> Investigating…
        </li>
      )}
    </ul>
  );
}

/** 参数可能很长（比如提交报告时的整份报告），摘要里只显示简短形式，完整内容点开才看。 */
function formatArgs(args: Record<string, unknown>): string {
  const text = Object.entries(args)
    .map(([key, value]) => `${key}=${typeof value === "string" ? value : JSON.stringify(value)}`)
    .join(" ");
  return text.length > 90 ? `${text.slice(0, 90)}…` : text;
}
