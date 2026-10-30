"use client";
/**
 * 顶部导航。需要知道"当前在哪个页面"来高亮对应的链接，而这个信息来自浏览器地址栏，
 * 所以必须是 Client Component（用 usePathname 这个 Hook）。
 */
import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Chat" },
  { href: "/jobs", label: "Automatic investigations" },
];

export function Nav() {
  const pathname = usePathname();
  return (
    <nav className="nav">
      <strong className="nav-brand">Infra Debugging Agent</strong>
      {LINKS.map(({ href, label }) => {
        // "/" 要精确匹配，否则所有页面都会高亮 Chat；其他链接匹配前缀，这样 /jobs/123 也算在 /jobs 下。
        const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
        return (
          // next/link 做客户端导航：不刷新整个页面，只替换变化的部分，比普通 <a> 快。
          <Link key={href} href={href} className={active ? "nav-link active" : "nav-link"}>
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
