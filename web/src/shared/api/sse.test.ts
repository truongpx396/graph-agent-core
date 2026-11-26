import { describe, expect, it, vi } from "vitest";
import { json } from "@/test/fakeApi";
import { apiFetch, bindRequestContext } from "./http";
import { readEventStream } from "./sse";

function chunked(...chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
  );
}

async function read(response: Response) {
  const events: unknown[] = [];
  for await (const event of readEventStream(response)) events.push(event);
  return events;
}

describe("readEventStream", () => {
  it("joins frames split across chunks and skips ids and keepalive comments", async () => {
    const response = chunked('id: 1\ndata: {"a"', ':1}\n\n: keepalive\n\nid: 2\r\ndata: {"a":2}\r\n\r\n', 'data: {"a":3}\n\n');
    expect(await read(response)).toEqual([{ a: 1 }, { a: 2 }, { a: 3 }]);
  });

  it("keeps a character split between two chunks whole", async () => {
    const bytes = new TextEncoder().encode('data: {"t":"é"}\n\n');
    const response = new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(bytes.slice(0, 13));
          controller.enqueue(bytes.slice(13));
          controller.close();
        },
      }),
    );
    expect(await read(response)).toEqual([{ t: "é" }]);
  });

  it("drops a frame cut off before its blank line", async () => {
    expect(await read(chunked('data: {"a":1}\n\ndata: {"a":'))).toEqual([{ a: 1 }]);
  });

  it("reports a refusal in the server's words", async () => {
    await expect(read(json({ code: "rate_limited", message: "Too many requests." }, 429))).rejects.toThrow("Too many requests.");
    await expect(read(new Response("", { status: 502 }))).rejects.toThrow("HTTP 502");
  });
});

describe("apiFetch", () => {
  it("adds the bound headers and reports a 401", async () => {
    const onUnauthorized = vi.fn<() => void>();
    const seen: RequestInit[] = [];
    bindRequestContext({ headers: () => ({ "X-Domain": "ops" }), onUnauthorized });
    vi.stubGlobal("fetch", async (_path: string, init: RequestInit) => {
      seen.push(init);
      return json({}, 401);
    });
    await apiFetch("/chat/cancel", { method: "POST", json: { thread_id: "t" } });
    expect(seen[0]?.headers).toEqual({ "X-Domain": "ops", "Content-Type": "application/json" });
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("refuses to send credentials to another origin", async () => {
    const fetched = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetched);
    await expect(apiFetch("//evil.example/chat")).rejects.toThrow("not a same-origin path");
    expect(fetched).not.toHaveBeenCalled();
  });
});
