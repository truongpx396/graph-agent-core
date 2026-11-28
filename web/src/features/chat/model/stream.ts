// Reading one turn's stream to its end, across dropped connections: the turn runs on a worker whether or not anyone is reading, and
// `GET /chat/stream/{request_id}` with `Last-Event-ID` picks it up where it stopped.
import { ApiError, type ChatEvent, readEventStream } from "@/shared/api";
import { rereadTurn } from "../api";

/** Pauses before each new attempt, in ms: about half a minute in all. */
export const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 15_000];

const wait = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    });
  });

export interface Reader {
  signal: AbortSignal;
  /** Whether the turn still wants events: false once it ended, paused or was left. */
  wanted: () => boolean;
  onEvent: (event: ChatEvent) => void;
  onReconnecting: (reconnecting: boolean) => void;
}

/**
 * Throws what finally went wrong. A response with no `X-Request-Id` cannot be read again, so its end is the end. A refusal (an
 * `ApiError`) is an answer, not a dropped connection, and is never retried.
 */
export async function follow(request: Promise<Response>, reader: Reader): Promise<void> {
  let requestId: string | null = null;
  let lastEventId: string | null = null;
  let failures = 0;
  let response = request;
  for (;;) {
    let problem: unknown = new Error("the connection closed before the reply finished");
    try {
      const reply = await response;
      requestId = reply.headers.get("X-Request-Id") ?? requestId;
      for await (const event of readEventStream<ChatEvent>(reply, (id) => (lastEventId = id))) {
        if (!reader.wanted()) return;
        failures = 0;
        reader.onReconnecting(false);
        reader.onEvent(event);
      }
      // The stream ended with the turn still open: a proxy or the network cut it.
      if (!reader.wanted() || requestId === null) return;
    } catch (error) {
      if (!reader.wanted()) return;
      if (requestId === null || error instanceof ApiError) throw error;
      problem = error;
    }
    const delay = RECONNECT_DELAYS[failures];
    if (delay === undefined) throw problem;
    failures += 1;
    reader.onReconnecting(true);
    await wait(delay, reader.signal);
    if (!reader.wanted()) return;
    response = rereadTurn(requestId, lastEventId, reader.signal);
  }
}
