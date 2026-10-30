import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, listJobs, sendMessage } from "./api";
import { authHeaders, clearApiKey, getApiKey, looksLikeApiKey, setApiKey, type KeyStorage } from "./auth";

function fakeStorage(): KeyStorage & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
    removeItem: (k) => void data.delete(k),
  };
}

const KEY = "agk_1a2b3c4d.AbC-123_xyz";

describe("api key storage", () => {
  it("saves a well-formed key (trimming whitespace) and reads it back", () => {
    const s = fakeStorage();
    expect(setApiKey(`  ${KEY}\n`, s)).toBe(true);
    expect(getApiKey(s)).toBe(KEY);
  });

  it.each(["", "   ", "agk_", "agk_abc", "agk_.x", "agk_zz.x", "Bearer agk_1a.b", "agk_1a.b c", "hello"])(
    "rejects a malformed key %j without saving it",
    (bad) => {
      const s = fakeStorage();
      expect(setApiKey(bad, s)).toBe(false);
      expect(s.data.size).toBe(0);
      expect(looksLikeApiKey(bad)).toBe(false);
    },
  );

  it("clearing removes the key", () => {
    const s = fakeStorage();
    setApiKey(KEY, s);
    clearApiKey(s);
    expect(getApiKey(s)).toBeNull();
  });

  it("builds a Bearer header only when a key exists", () => {
    const s = fakeStorage();
    expect(authHeaders(s)).toEqual({});
    setApiKey(KEY, s);
    expect(authHeaders(s)).toEqual({ Authorization: `Bearer ${KEY}` });
  });

  it("does not crash when there is no storage (server-side rendering)", () => {
    expect(getApiKey(null)).toBeNull();
    expect(setApiKey(KEY, null)).toBe(false);
    expect(authHeaders(null)).toEqual({});
  });
});

describe("api calls", () => {
  afterEach(() => vi.unstubAllGlobals());

  function stubBrowser(status: number, body: unknown = {}, withKey = true) {
    const s = fakeStorage();
    if (withKey) setApiKey(KEY, s);
    vi.stubGlobal("window", { sessionStorage: s });
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status }));
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("sends the key on every kind of request, never in the URL", async () => {
    const fetchMock = stubBrowser(200, []);
    await listJobs();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>).Authorization).toBe(`Bearer ${KEY}`);
    expect(url).not.toContain("agk_");
  });

  it("sends the key on the streaming request too", async () => {
    const fetchMock = stubBrowser(404, { detail: "session not found" });
    await expect(sendMessage("s1", "hi", { onStep() {}, onTurn() {} })).rejects.toBeInstanceOf(ApiError);
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>).Authorization).toBe(`Bearer ${KEY}`);
  });

  it("turns 401 and 403 into actionable messages", async () => {
    stubBrowser(401, { detail: "invalid or missing API key" }, false);
    await expect(listJobs()).rejects.toMatchObject({ status: 401, message: expect.stringContaining("top bar") });
    stubBrowser(403, { detail: "x" });
    await expect(listJobs()).rejects.toMatchObject({ status: 403, message: expect.stringContaining("not allowed") });
  });
});
