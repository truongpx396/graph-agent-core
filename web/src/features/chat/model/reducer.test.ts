import { describe, expect, it } from "vitest";
import type { ChatEvent } from "@/shared/api";
import { assistantEntry } from "./entry";
import { applyEvent } from "./reducer";

const after = (...events: ChatEvent[]) => events.reduce(applyEvent, assistantEntry("a"));
const token = (content: string): ChatEvent => ({ type: "token", content });
const start = (tool: string): ChatEvent => ({ type: "tool_start", tool, args: {} });

describe("applyEvent", () => {
  it("streams into the draft and commits it once the turn is done", () => {
    expect(after(token("Hel"), token("lo"))).toMatchObject({ draft: "Hello", answer: "", status: null });
    expect(after(token("Hello"), { type: "done" })).toMatchObject({ draft: "", answer: "Hello", status: null });
  });

  it("throws away only the draft on a retry", () => {
    expect(after(token("bad"), { type: "retry" })).toMatchObject({ draft: "", answer: "", status: "Revising answer" });
  });

  it("commits text from before AND after an approval pause", () => {
    const paused = after(token("I'll remember that."), { type: "approval_required", tool_calls: [{ name: "remember", args: {} }] });
    expect(paused).toMatchObject({ answer: "I'll remember that.", approval: [{ name: "remember" }] });
    const done = [token("Saved."), { type: "done" } as const].reduce(applyEvent, paused);
    expect(done).toMatchObject({ answer: "I'll remember that.\n\nSaved.", draft: "", approval: null });
  });

  it("marks the matching running tool done", () => {
    const entry = after(start("a"), start("b"), start("a"), { type: "tool_end", tool: "a" });
    expect(entry.tools.map((item) => item.done)).toEqual([false, false, true]);
    expect(entry.status).toBe("Thinking");
  });

  it("offers a clarification's options and sets the restatement back", () => {
    const entry = after({ type: "tool_start", tool: "ask_clarification", args: { question: "Which?", options: ["A", "B"] } });
    expect(entry).toMatchObject({ clarification: { question: "Which?", options: ["A", "B"] }, muted: true });
  });

  it.each([
    ["cancelled", "Stopped before finishing."],
    ["internal_error", "Interrupted: boom"],
  ])("says a partial answer did not finish (%s)", (code, status) => {
    expect(after(token("half"), { type: "error", content: "boom", code })).toMatchObject({ answer: "half", status, notice: null });
  });

  it("says why there is no answer at all", () => {
    expect(after({ type: "error", content: "boom" })).toMatchObject({ answer: "", notice: "[error: boom]", status: null });
    expect(after({ type: "error", content: "x", code: "cancelled" }).notice).toBe("Stopped.");
  });

  it("clears the status on done even when nothing was streamed", () => {
    expect(after({ type: "tool_end", tool: "a" }, { type: "done" }).status).toBeNull();
  });

  it("ignores an event type it does not know", () => {
    expect(after({ type: "usage" } as unknown as ChatEvent)).toEqual(assistantEntry("a"));
  });
});
