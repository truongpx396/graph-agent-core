import { errorFrom } from "./http";

// `EventSource` can neither POST nor send headers, so the streams are read from `fetch`'s body (docs/RUNTIME_PATTERNS.md pattern 29).

/** One frame: its `data:` lines joined, and its `id:`. A comment (`: keepalive`) has neither. */
function partsOf(frame: string): { data: string | null; id: string | null } {
  const lines = frame.split(/\r?\n/);
  const field = (name: string) => lines.filter((line) => line.startsWith(`${name}:`)).map((line) => line.slice(line.startsWith(`${name}: `) ? name.length + 2 : name.length + 1));
  const data = field("data");
  return { data: data.length === 0 ? null : data.join("\n"), id: field("id").at(-1) ?? null };
}

/** `onId` is told each delivered event's position: what `Last-Event-ID` carries when a dropped stream is read again. */
export async function* readEventStream<T>(response: Response, onId?: (id: string) => void): AsyncGenerator<T, void, void> {
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
        const { data, id } = partsOf(frame);
        if (data === null) continue;
        const event = JSON.parse(data) as T;
        if (id !== null) onId?.(id);
        yield event;
      }
      if (done) return;
    }
  } finally {
    // Also when the consumer stops early: otherwise the connection stays open.
    await reader.cancel().catch(() => {});
  }
}
