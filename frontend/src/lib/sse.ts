/**
 * 解析 SSE（Server-Sent Events）文本流。
 *
 * 为什么要手写，而不用浏览器自带的 EventSource？
 * EventSource 只支持 GET 请求，而我们的流式接口是 POST（要带消息内容）。
 * 所以改用 fetch，自己把响应体当作流来读，再按 SSE 格式切分。
 *
 * SSE 的格式：一个事件由若干行组成，以【空行】结束：
 *     event: step
 *     data: {"tool": "search_logs"}
 *     <空行>
 *
 * 【难点】网络数据是一块一块（chunk）到达的，块的边界和事件的边界没有关系。
 * 一个事件可能被切成两块，也可能一块里有好几个事件。
 * 所以必须用缓冲区：把收到的文本不断追加进去，只处理"已经完整"的事件，剩下的留着等下一块。
 */

export interface SseEvent {
  event: string;
  data: string;
}

/** 解析一个完整的事件块（不含结尾的空行）。没有 data 的块（例如纯注释）返回 null。 */
export function parseBlock(block: string): SseEvent | null {
  let event = "message"; // SSE 规范：没写 event 行时，事件名默认是 "message"
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue; // 以冒号开头是注释，常用来做心跳保活
    const colon = line.indexOf(":");
    if (colon === -1) continue;
    const field = line.slice(0, colon);
    // 冒号后面如果紧跟一个空格，规范要求去掉它。
    const value = line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value); // 多行 data 要用换行符拼起来
  }
  return dataLines.length > 0 ? { event, data: dataLines.join("\n") } : null;
}

/**
 * 把字节流转成事件序列。
 *
 * async function* 是"异步生成器"：每次 yield 产出一个值，调用方用 `for await` 逐个消费。
 * 可以类比 Java 里一个会阻塞等待下一个元素的迭代器（Iterator）。
 */
export async function* parseSse(stream: ReadableStream<Uint8Array>): AsyncGenerator<SseEvent> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      // stream: true 表示"后面还有数据"：一个汉字占多个字节，可能被切在两个块之间，
      // 这个选项让解码器把不完整的字节先留着，等下一块来了再拼上，避免出现乱码。
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");

      // 只要缓冲区里有空行，就说明至少有一个完整的事件。
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const parsed = parseBlock(block);
        if (parsed) yield parsed;
      }
    }
    // 流结束了：把解码器里残留的内容冲出来；如果最后一个事件没有以空行结尾，也别丢掉。
    buffer += decoder.decode();
    const tail = parseBlock(buffer.trim());
    if (tail) yield tail;
  } finally {
    reader.releaseLock(); // 释放对流的锁，类似 Java 的 finally 里关闭资源
  }
}
