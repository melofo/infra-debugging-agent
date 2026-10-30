"use client";
/**
 * 轮询：每隔一段时间重新请求一次数据，让页面保持最新。
 *
 * React 里"自定义 Hook"就是一个以 use 开头的函数，里面可以用 useState / useEffect。
 * 它把"定时请求 + 清理"这类可复用的逻辑从组件里抽出来（类似 Java 里抽出一个工具类）。
 *
 * stopWhen：拿到数据后判断"还需要继续轮询吗"。返回 true 就停止（比如任务已经结束，不会再变了）。
 * 为什么放在 Hook 里面，而不是让调用方用 enabled 开关？因为"是否结束"要看刚拿到的数据，
 * 而数据就是这个 Hook 返回的，调用方在 Hook 调用之前拿不到它，会形成循环。
 * 注意 stopWhen 必须是稳定的引用（模块级函数，或者用 useCallback 包过），否则每次渲染都会重启轮询。
 */
import { useEffect, useState } from "react";

export function usePolling<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  intervalMs: number,
  stopWhen?: (data: T) => boolean,
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();

    async function tick() {
      let finished = false;
      // 页面在后台标签页时不请求，省得白白消耗后端和流量；回到前台后下一轮自动恢复。
      if (!document.hidden) {
        try {
          const value = await fetcher(controller.signal);
          if (!cancelled) {
            setData(value);
            setError(null);
            finished = stopWhen?.(value) ?? false;
          }
        } catch (e) {
          if (!cancelled) setError(e instanceof Error ? e : new Error(String(e)));
        } finally {
          if (!cancelled) setLoading(false);
        }
      }
      // 用"请求结束后再设下一个 setTimeout"，而不是 setInterval：
      // 请求慢的时候不会堆积重叠的请求，后端慢了也不会被我们越压越慢。
      if (!cancelled && !finished) timer = setTimeout(tick, intervalMs);
    }
    tick();

    // useEffect 返回的函数是"清理函数"：组件卸载，或依赖变化重新执行之前调用。
    // 必须取消定时器和进行中的请求，否则离开页面后它们还会继续跑（内存泄漏、往已卸载的组件里写状态）。
    return () => {
      cancelled = true;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [fetcher, intervalMs, stopWhen]);

  return { data, error, loading };
}
