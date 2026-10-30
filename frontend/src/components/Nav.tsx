"use client";
/**
 * 顶部导航。需要知道"当前在哪个页面"来高亮对应的链接，而这个信息来自浏览器地址栏，
 * 所以必须是 Client Component（用 usePathname 这个 Hook）。
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { clearApiKey, getApiKey, setApiKey } from "@/lib/auth";

const LINKS = [
  { href: "/", label: "Chat" },
  { href: "/jobs", label: "Automatic investigations" },
];

/**
 * API 密钥输入。密钥只存在 sessionStorage（见 lib/auth.ts），输入框是 password 类型，不会明文显示。
 *
 * 为什么在 useEffect 里才读密钥：服务端渲染时没有 sessionStorage。如果渲染时直接读，
 * 服务端输出"没有密钥"、浏览器却渲染"有密钥"，两边不一致会触发 hydration 报错。
 * 所以先按"未知"渲染，挂载到浏览器之后再读（useEffect 只在浏览器里执行）。
 */
function ApiKeyBar() {
  const [hasKey, setHasKey] = useState<boolean | null>(null);
  const [draft, setDraft] = useState("");
  const [invalid, setInvalid] = useState(false);

  useEffect(() => {
    // 挂载后读浏览器存储（见上面的说明），这正是"和外部系统同步"这类场景。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setHasKey(getApiKey() !== null);
  }, []);

  if (hasKey === null) return null;

  // 保存或清除之后整页刷新：让所有正在轮询的请求和聊天会话都用新密钥重新开始，比逐个通知组件更不容易出错。
  if (hasKey) {
    return (
      <span className="nav-key">
        API key set
        <button
          type="button"
          onClick={() => {
            clearApiKey();
            window.location.reload();
          }}
        >
          Clear
        </button>
      </span>
    );
  }
  return (
    <form
      className="nav-key"
      onSubmit={(e) => {
        e.preventDefault();
        if (setApiKey(draft)) window.location.reload();
        else setInvalid(true);
      }}
    >
      <input
        type="password"
        value={draft}
        placeholder="API key (agk_...)"
        autoComplete="off"
        aria-invalid={invalid}
        onChange={(e) => {
          setDraft(e.target.value);
          setInvalid(false);
        }}
      />
      <button type="submit">Save</button>
      {invalid && <span className="nav-key-error">Not a valid key format</span>}
    </form>
  );
}

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
      <ApiKeyBar />
    </nav>
  );
}
