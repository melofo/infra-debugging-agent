/**
 * 浏览器端的 API 密钥管理。
 *
 * 【密钥放哪里？】放 sessionStorage：关闭标签页就消失，也不会发给任何其他网站。
 * 取舍：localStorage 会永久保留，用起来方便，但密钥长期躺在磁盘上、且被同源的任何脚本读到；
 * 内存变量最安全，但刷新页面就丢。sessionStorage 是折中。
 * 注意：只要页面上有 XSS（被注入的脚本），任何浏览器端存储都读得到密钥，
 * 所以真正的防线是"不引入不可信脚本 + 密钥权限最小 + 随时可以吊销"。
 * （生产环境更好的做法是让后端签发 HttpOnly Cookie 会话，脚本根本读不到；这里是第一版的简化。）
 *
 * 【为什么不能用 NEXT_PUBLIC_ 环境变量放密钥】那种变量在构建时被写死进发给所有人的 JS 里，等于公开。
 */

const STORAGE_KEY = "agent_api_key";

/** 只需要这三个方法，方便测试里换成假的（浏览器的 Storage 满足它）。 */
export interface KeyStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function browserStorage(): KeyStorage | null {
  try {
    // 服务端渲染时没有 window；隐私模式下访问 sessionStorage 也可能抛异常。
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

/** 密钥格式：agk_<十六进制 id>.<url 安全的随机串>。只做格式检查，真假由后端判断。 */
export function looksLikeApiKey(value: string): boolean {
  return /^agk_[0-9a-f]+\.[A-Za-z0-9_-]+$/.test(value);
}

export function getApiKey(storage: KeyStorage | null = browserStorage()): string | null {
  return storage?.getItem(STORAGE_KEY) ?? null;
}

/** 保存前先整理空白并检查格式；格式不对返回 false，不保存。 */
export function setApiKey(raw: string, storage: KeyStorage | null = browserStorage()): boolean {
  const key = raw.trim();
  if (!storage || !looksLikeApiKey(key)) return false;
  storage.setItem(STORAGE_KEY, key);
  return true;
}

export function clearApiKey(storage: KeyStorage | null = browserStorage()): void {
  storage?.removeItem(STORAGE_KEY);
}

/** 每个请求都带上它：有密钥就是 { Authorization: "Bearer ..." }，没有就是空对象。 */
export function authHeaders(storage: KeyStorage | null = browserStorage()): Record<string, string> {
  const key = getApiKey(storage);
  return key ? { Authorization: `Bearer ${key}` } : {};
}
