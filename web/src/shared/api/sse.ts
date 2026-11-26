import { errorFrom } from "./http";

// `EventSource` can neither POST nor send headers, so the streams are read from `fetch`'s body (docs/RUNTIME_PATTERNS.md pattern 29).

/** The payload of one frame: its `data:` lines joined. Comments (`: keepalive`) and `id:` lines carry none. */
function dataOf(frame: string): string | null {
  const lines = frame.split(/\r?\n/).filter((line) => line.startsWith("data:"));
  if (lines.length === 0) return null;
  return lines.map((line) => line.slice(line.startsWith("data: ") ? 6 : 5)).join("\n");
}

export async function* readEventStream<T>(response: Response): AsyncGenerator<T, void, void> {
  if (!response.ok) throw await errorFrom(response);
  if (!response.body) throw new Error("the response has no body");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      const frames = buffer.split(/\r?\n\r?\n/);
      // The last piece is a frame still being received; at the end of the stream it is whatever never got its blank line.
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const data = dataOf(frame);
        if (data !== null) yield JSON.parse(data) as T;
      }
      if (done) return;
    }
  } finally {
    // Also when the consumer stops early: otherwise the connection stays open.
    await reader.cancel().catch(() => {});
  }
}
