// 动态路由：文件夹名 [id] 表示这一段 URL 是变量。/jobs/abc 里的 abc 就是 id。
// 对应 Spring 的 @GetMapping("/jobs/{id}") + @PathVariable。
//
// 这个页面本身是 Server Component（默认），它不能用 useState，也不能用浏览器 API。
// Next.js 16 里路径参数是"运行时数据"（构建时不知道），必须放在 <Suspense> 里读取，
// 加载期间显示 fallback。真正的界面和轮询在 Client Component（JobDetailView）里。
import { Suspense } from "react";
import { JobDetailView } from "@/components/JobDetailView";

export default function Page({ params }: PageProps<"/jobs/[id]">) {
  return (
    <Suspense fallback={<main className="page-content"><p className="subtitle">Loading…</p></main>}>
      {params.then(({ id }) => (
        <JobDetailView jobId={id} />
      ))}
    </Suspense>
  );
}
