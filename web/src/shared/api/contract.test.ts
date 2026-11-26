// The client against openapi.json: an event the server gains must fail here, not be silently ignored in a browser.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import type { ChatEvent, IngestEvent } from "./types";

const document = JSON.parse(readFileSync(resolve(import.meta.dirname, "../../../../openapi.json"), "utf8"));

const published = (path: string, method: string): string[] =>
  Object.keys(document.paths[path][method].responses["200"].content["text/event-stream"].itemSchema.properties.data.contentSchema.discriminator.mapping);

// `Record<union, true>`: the compiler rejects a missing or extra key, so these lists cannot drift from the types.
const CHAT: Record<ChatEvent["type"], true> = {
  token: true, tool_start: true, tool_end: true, retry: true, compacted: true, citations: true, followups: true,
  approval_required: true, done: true, error: true, system_note: true,
};
const INGEST: Record<IngestEvent["type"], true> = { started: true, progress: true, done: true, error: true };

describe("the stream contract", () => {
  it.each([
    ["/chat/stream/queued", "post"],
    ["/chat/resume", "post"],
    ["/chat/cancel", "post"],
  ])("handles every event %s publishes", (path, method) => {
    // system_note is sent but not published (see types.ts); nothing else may differ.
    expect(Object.keys(CHAT).toSorted()).toEqual([...published(path, method), "system_note"].toSorted());
  });

  it("handles every ingest event", () => {
    expect(Object.keys(INGEST).toSorted()).toEqual(published("/ingest/stream/{job_id}", "get").toSorted());
  });
});
