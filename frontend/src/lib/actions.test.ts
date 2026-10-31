import { afterEach, describe, expect, it, vi } from "vitest";
import { approveAction, listActions, rejectAction } from "./api";
import { setApiKey, type KeyStorage } from "./auth";

const KEY = "agk_1a2b3c4d.AbC-123_xyz";
const HASH = "a".repeat(64);

function stub(status: number, body: unknown = {}) {
  const data = new Map<string, string>();
  const storage: KeyStorage = {
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
    removeItem: (k) => void data.delete(k),
  };
  setApiKey(KEY, storage);
  vi.stubGlobal("window", { sessionStorage: storage });
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function lastCall(fetchMock: ReturnType<typeof stub>) {
  const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  return { url, init, headers: init.headers as Record<string, string> };
}

describe("approval api calls", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("approving sends the reviewed content hash and the api key in the header", async () => {
    const fetchMock = stub(200, { status: "executed" });
    await approveAction("abc", HASH, "ok");
    const { url, init, headers } = lastCall(fetchMock);
    expect(url).toMatch(/\/actions\/abc\/approve$/);
    expect(init.method).toBe("POST");
    expect(headers.Authorization).toBe(`Bearer ${KEY}`);
    expect(JSON.parse(init.body as string)).toEqual({ content_hash: HASH, note: "ok" });
  });

  it("an empty note is sent as null", async () => {
    const fetchMock = stub(200);
    await rejectAction("abc", "");
    expect(JSON.parse(lastCall(fetchMock).init.body as string)).toEqual({ note: null });
  });

  it("ids are url-encoded so they cannot change the path", async () => {
    const fetchMock = stub(200);
    await approveAction("../jobs", HASH);
    expect(lastCall(fetchMock).url).toContain("/actions/..%2Fjobs/approve");
  });

  it("surfaces the server's explanation when the content changed (409)", async () => {
    stub(409, { detail: "the content changed since you reviewed it; reload and review it again" });
    await expect(approveAction("abc", HASH)).rejects.toMatchObject({ status: 409, message: expect.stringContaining("changed") });
  });

  it("a read-only key gets an actionable message on 403", async () => {
    stub(403, { detail: "x" });
    await expect(approveAction("abc", HASH)).rejects.toMatchObject({ status: 403, message: expect.stringContaining("not allowed") });
  });

  it("listing can filter by status", async () => {
    const fetchMock = stub(200, []);
    await listActions("pending");
    expect(lastCall(fetchMock).url).toMatch(/\/actions\?status=pending$/);
  });
});
