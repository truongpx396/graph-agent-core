// A fake `fetch` for the API: no network. Streams are real ReadableStreams, so the app's own SSE parsing runs.
import { vi } from "vitest";

export interface Call {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: unknown;
}

export const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

export const sseText = (events: object[]) => events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");

export const sse = (...events: object[]) => new Response(sseText(events), { headers: { "Content-Type": "text/event-stream" } });

/** A stream the test feeds by hand, to act while a turn is still in flight. */
export function openStream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start: (c) => void (controller = c) });
  const encoder = new TextEncoder();
  return {
    response: new Response(body),
    send: (...events: object[]) => controller.enqueue(encoder.encode(sseText(events))),
    close: () => controller.close(),
  };
}

/** A response the test releases when it chooses, to order two requests. */
export function deferred() {
  let release!: (response: Response) => void;
  const response = new Promise<Response>((resolve) => void (release = resolve));
  return { response, release };
}

type Handler = (call: Call) => Response | Promise<Response>;

/** Routes are keyed "METHOD /path" with the query string removed. Anything unlisted is a 404. */
export function fakeApi(routes: Record<string, Handler>) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", async (input: string, init: RequestInit = {}) => {
    const call: Call = {
      method: init.method ?? "GET",
      url: input,
      headers: (init.headers ?? {}) as Record<string, string>,
      body: typeof init.body === "string" && init.body.startsWith("{") ? JSON.parse(init.body) : (init.body ?? null),
    };
    calls.push(call);
    const handler = routes[`${call.method} ${input.split("?")[0] ?? ""}`];
    return handler ? handler(call) : json({ detail: "not found" }, 404);
  });
  return { calls, sent: (route: string) => calls.filter((call) => `${call.method} ${call.url.split("?")[0] ?? ""}` === route) };
}
