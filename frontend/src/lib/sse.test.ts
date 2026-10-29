/**
 * SSE 解析器的测试。vitest 的写法和 JUnit 很像：describe 分组，it/test 是一个用例，expect 做断言。
 * 重点测"事件被网络切断"的各种情况，这是 SSE 解析最容易出错的地方。
 */
import { describe, expect, it } from "vitest";
import { parseBlock, parseSse, type SseEvent } from "./sse";

/** 构造一个模拟的网络流：每个字符串是一个到达的数据块。 */
function streamOf(chunks: (string | Uint8Array)[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(typeof chunk === "string" ? encoder.encode(chunk) : chunk);
      }
      controller.close();
    },
  });
}

async function collect(stream: ReadableStream<Uint8Array>): Promise<SseEvent[]> {
  const events: SseEvent[] = [];
  for await (const event of parseSse(stream)) events.push(event);
  return events;
}

describe("parseBlock", () => {
  it("reads event name and data", () => {
    expect(parseBlock('event: step\ndata: {"a":1}')).toEqual({ event: "step", data: '{"a":1}' });
  });

  it("defaults the event name to 'message'", () => {
    expect(parseBlock("data: hi")).toEqual({ event: "message", data: "hi" });
  });

  it("joins multi-line data with newlines", () => {
    expect(parseBlock("data: a\ndata: b")).toEqual({ event: "message", data: "a\nb" });
  });

  it("ignores comments and blocks without data", () => {
    expect(parseBlock(": keep-alive")).toBeNull();
    expect(parseBlock("event: step")).toBeNull();
  });
});

describe("parseSse", () => {
  it("parses several events delivered in one chunk", async () => {
    const events = await collect(streamOf(["event: step\ndata: 1\n\nevent: turn\ndata: 2\n\n"]));
    expect(events).toEqual([
      { event: "step", data: "1" },
      { event: "turn", data: "2" },
    ]);
  });

  it("reassembles an event split across chunks", async () => {
    const events = await collect(streamOf(["event: st", "ep\nda", "ta: hello\n", "\nevent: turn\ndata: x\n\n"]));
    expect(events).toEqual([
      { event: "step", data: "hello" },
      { event: "turn", data: "x" },
    ]);
  });

  it("handles the blank line separator arriving in a separate chunk", async () => {
    const events = await collect(streamOf(["event: step\ndata: 1\n", "\n"]));
    expect(events).toEqual([{ event: "step", data: "1" }]);
  });

  it("does not corrupt multi-byte characters split between chunks", async () => {
    const bytes = new TextEncoder().encode("event: turn\ndata: 连接池\n\n");
    // 在汉字"连"的字节中间切开（一个汉字占 3 个字节）。
    const cut = bytes.indexOf(0xe8) + 1;
    const events = await collect(streamOf([bytes.slice(0, cut), bytes.slice(cut)]));
    expect(events).toEqual([{ event: "turn", data: "连接池" }]);
  });

  it("emits a final event even if the stream ends without a blank line", async () => {
    const events = await collect(streamOf(["event: turn\ndata: last"]));
    expect(events).toEqual([{ event: "turn", data: "last" }]);
  });

  it("understands CRLF line endings", async () => {
    const events = await collect(streamOf(["event: step\r\ndata: 1\r\n\r\n"]));
    expect(events).toEqual([{ event: "step", data: "1" }]);
  });
});
