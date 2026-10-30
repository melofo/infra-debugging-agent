import type { Metadata } from "next";
import { Suspense } from "react";
import { Geist, Geist_Mono } from "next/font/google";
import { Nav } from "@/components/Nav";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Infra Debugging Agent",
  description: "AI agent that investigates production incidents and explains root causes with evidence",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable}`}>
      <body>
        <div className="shell">
          {/* Nav 用了 usePathname()。在 /jobs/[id] 这种"路径参数构建时未知"的页面上，路径要到请求时才知道，
              所以它必须放在 Suspense 里：构建时先渲染 fallback（一个占位的导航栏），请求时再换成真实的。
              不加的话，生产构建会失败（CLIENT_HOOK_DYNAMIC）。 */}
          <Suspense fallback={<nav className="nav"><strong className="nav-brand">Infra Debugging Agent</strong></nav>}>
            <Nav />
          </Suspense>
          <div className="page">{children}</div>
        </div>
      </body>
    </html>
  );
}
