import { describe, expect, it } from "vitest";
import type { ChatEvent } from "@/shared/api";
import { assistantEntry } from "./entry";
import { applyEvent } from "./reducer";

const after = (...events: ChatEvent[]) => events.reduce(applyEvent, assistantEntry("a"));
const token = (content: string): ChatEvent => ({ type: "token", content });
const start = (tool: string, call_id?: string, more: object = {}): ChatEvent => ({ type: "tool_start", tool, args: {}, call_id, label: `Do ${tool}`, ...more });
const status = (state: string): ChatEvent => ({ type: "status", state, at: "2026-11-28T00:00:00Z" });
const failed = (code: string, kind: string, content = "server words"): ChatEvent => ({ type: "error", content, code, details: { kind } });

describe("applyEvent", () => {
  it("streams into the draft and commits it once the turn is done", () => {
    expect(after(token("Hel"), token("lo"))).toMatchObject({ draft: "Hello", answer: "", status: null });
    expect(after(token("Hello"), { type: "done" })).toMatchObject({ draft: "", answer: "Hello", status: null, done: true });
  });

  it("knows a worker has the job only from something a worker wrote", () => {
    expect(after(status("queued")).started).toBe(false);
    expect(after(status("queued"), status("started")).started).toBe(true);
    // A server from before the status event: its first event is the sign.
    expect(after({ type: "compacted" }).started).toBe(true);
  });

  it("throws away only the draft on a retry", () => {
    expect(after(token("bad"), { type: "retry" })).toMatchObject({ draft: "", answer: "", status: "Rewriting the answer…" });
  });

  it("commits text from before AND after an approval pause, and keeps the pause's identity", () => {
    const pause: ChatEvent = { type: "approval_required", tool_calls: [{ name: "remember", args: {} }], approval_id: "apr_1", items: [] };
    const paused = after(token("I'll remember that."), pause);
    expect(paused).toMatchObject({ answer: "I'll remember that.", approval: { id: "apr_1", calls: [{ name: "remember" }] } });
    const done = [token("Saved."), { type: "done" } as const].reduce(applyEvent, paused);
    expect(done).toMatchObject({ answer: "I'll remember that.\n\nSaved.", draft: "", approval: null });
  });

  it("pairs parallel calls of one tool by their call id", () => {
    const entry = after(start("look", "c1"), start("look", "c2"), {
      type: "tool_end", tool: "look", call_id: "c1", outcome: "refused", duration_ms: 12, summary: null,
    });
    expect(entry.steps.map((step) => [step.callId, step.outcome, step.durationMs])).toEqual([["c1", "refused", 12], ["c2", "running", null]]);
  });

  it("copes with a server that sends no call id or label: the last running call of the tool, and no identifier shown", () => {
    const old: ChatEvent = { type: "tool_start", tool: "secret_tool", args: {} };
    const entry = after(old, old, { type: "tool_end", tool: "secret_tool" });
    expect(entry.steps.map((step) => step.outcome)).toEqual(["running", "ok"]);
    expect(entry.steps.map((step) => step.label)).toEqual(["A step", "A step"]);
  });

  it("stops a step still running when the turn breaks off, so nothing animates for ever", () => {
    expect(after(start("look", "c1"), failed("timeout", "failed")).steps[0]?.outcome).toBe("error");
  });

  it("offers a clarification's options and sets the restatement back", () => {
    const entry = after({ type: "tool_start", tool: "ask_clarification", args: { question: "Which?", options: ["A", "B"] } });
    expect(entry).toMatchObject({ clarification: { question: "Which?", options: ["A", "B"] }, muted: true });
  });

  it.each([
    [failed("tenant_budget_exceeded", "blocked", "Monthly budget reached."), { tone: "blocked", text: "Monthly budget reached.", hint: "A workspace admin can raise the limit." }],
    [failed("internal", "failed", "Something went wrong."), { tone: "failed", text: "Something went wrong." }],
    [failed("worker_unreachable", "lost", "No response for 'abc' after 30s"), { tone: "lost", text: "Nothing picked up your message. Try again in a moment." }],
    [{ type: "error", content: "Cancelled by user.", code: "cancelled" } as const, { tone: "plain", text: "Stopped." }],
    [{ type: "error", content: "boom" } as const, { tone: "failed", text: "boom" }],
  ])("says what kind of end it was and what to do (%#)", (event, notice) => {
    expect(after(event)).toMatchObject({ notice, status: null, done: true });
  });

  it("never lets a partial answer pass for a whole one", () => {
    expect(after(token("half"), { type: "error", content: "x", code: "cancelled" })).toMatchObject({ answer: "half", notice: { text: "Stopped before finishing." } });
    expect(after(token("half"), failed("internal", "failed", "It broke."))).toMatchObject({ notice: { text: "Not finished. It broke." } });
  });

  it("keeps what the turn used, with unknown staying unknown", () => {
    const usage: ChatEvent = { type: "usage", model: "m", route: "local", input_tokens: null, output_tokens: 3, cost_usd: null, model_calls: 1 };
    expect(after(usage).usage).toEqual({ model: "m", route: "local", tokens: null, costUsd: null, calls: 1 });
  });

  it("ignores an event type it does not know", () => {
    expect(after({ type: "novel" } as unknown as ChatEvent)).toEqual(assistantEntry("a"));
  });
});
