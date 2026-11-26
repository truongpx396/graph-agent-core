import { beforeEach, describe, expect, it } from "vitest";
import { bindRequestContext } from "@/shared/api";
import { fakeApi, json, openStream, sse } from "@/test/fakeApi";
import type { AssistantEntry } from "./entry";
import { useChat } from "./store";

const chat = () => useChat.getState();
const reply = () => chat().entries.findLast((entry) => entry.kind === "assistant") as AssistantEntry;
const pause = { type: "approval_required", tool_calls: [{ name: "remember", args: { content: "x" } }] };

beforeEach(() => bindRequestContext({ headers: () => ({}), onUnauthorized: () => {} }));

describe("a turn", () => {
  it("sends the message, thread and image, and releases the page when done", async () => {
    const api = fakeApi({ "POST /chat/stream/queued": () => sse({ type: "token", content: "42" }, { type: "done" }) });
    await chat().send("21*2?", { uri: "data:image/png;base64,AA==", label: "cat.png" });
    expect(api.calls[0]?.body).toEqual({ message: "21*2?", thread_id: chat().threadId, images: ["data:image/png;base64,AA=="] });
    expect(chat().entries[0]).toMatchObject({ kind: "user", text: "21*2?\n[attached image: cat.png]" });
    expect(reply().answer).toBe("42");
    expect(chat().turn).toBeNull();
  });

  it("releases the page when the stream just ends", async () => {
    fakeApi({ "POST /chat/stream/queued": () => sse({ type: "token", content: "half" }) });
    await chat().send("hi");
    expect(reply()).toMatchObject({ answer: "half", status: null });
    expect(chat().turn).toBeNull();
  });

  it("shows why a refused request failed", async () => {
    fakeApi({ "POST /chat/stream/queued": () => json({ message: "Monthly budget reached." }, 402) });
    await chat().send("hi");
    expect(reply().notice).toBe("[error: Monthly budget reached.]");
    expect(chat().turn).toBeNull();
  });

  it("adds a system note as its own line", async () => {
    fakeApi({ "POST /chat/stream/queued": () => sse({ type: "system_note", content: "starting a new request" }, { type: "done" }) });
    await chat().send("hi");
    expect(chat().entries.at(-1)).toMatchObject({ kind: "system", text: "starting a new request" });
  });

  it("allows only one at a time", async () => {
    const stream = openStream();
    const api = fakeApi({ "POST /chat/stream/queued": () => stream.response });
    const first = chat().send("one");
    await chat().send("two");
    stream.send({ type: "done" });
    stream.close();
    await first;
    expect(api.calls).toHaveLength(1);
  });

  it("drops events of a conversation that was left, without disturbing the next one", async () => {
    const old = openStream();
    const next = openStream();
    const responses = [old.response, next.response];
    fakeApi({ "POST /chat/stream/queued": () => responses.shift() ?? json({}, 500) });
    const first = chat().send("old");
    chat().startFresh();
    const second = chat().send("new");
    old.send({ type: "token", content: "stale" }, { type: "done" });
    old.close();
    await first;
    expect(chat().turn).not.toBeNull(); // the old turn's `done` must not end the new turn
    expect(reply().draft).toBe("");
    next.send({ type: "done" });
    next.close();
    await second;
  });
});

describe("an approval", () => {
  it("pauses, then resumes once with the decision", async () => {
    const api = fakeApi({
      "POST /chat/stream/queued": () => sse(pause),
      "POST /chat/resume": () => sse({ type: "token", content: "Saved." }, { type: "done" }),
    });
    await chat().send("remember x");
    expect(chat().turn?.phase).toBe("paused");
    await Promise.all([chat().decide(true), chat().decide(true)]); // a double click
    expect(api.sent("POST /chat/resume").map((call) => call.body)).toEqual([{ thread_id: chat().threadId, approved: true }]);
    expect(reply()).toMatchObject({ answer: "Saved.", approval: null });
    expect(chat().turn).toBeNull();
  });

  it("stays paused when the resumed turn asks again", async () => {
    // The page this replaces finished the turn here: Stop disappeared while a second approval was on screen.
    fakeApi({ "POST /chat/stream/queued": () => sse(pause), "POST /chat/resume": () => sse(pause) });
    await chat().send("remember x");
    await chat().decide(false);
    expect(chat().turn?.phase).toBe("paused");
    expect(reply().approval).toHaveLength(1);
  });

  it("is cancelled by Stop through the cancel stream", async () => {
    fakeApi({
      "POST /chat/stream/queued": () => sse(pause),
      "POST /chat/cancel": () => sse({ type: "error", code: "cancelled", content: "cancelled" }),
    });
    await chat().send("remember x");
    await chat().stop();
    expect(reply()).toMatchObject({ approval: null, notice: "Stopped." });
    expect(chat().turn).toBeNull();
  });

  it("is restored after a reload, unless this build cannot resume it", () => {
    chat().open("t1");
    chat().restorePendingApproval("t1", { tool_calls: pause.tool_calls, resumable: true });
    expect(chat().turn?.phase).toBe("paused");
    chat().open("t2");
    chat().restorePendingApproval("t1", { tool_calls: pause.tool_calls, resumable: true }); // arrived late
    expect(chat().entries).toEqual([]);
    chat().restorePendingApproval("t2", { tool_calls: pause.tool_calls, resumable: false });
    expect(chat().turn).toBeNull();
    expect(reply().notice).toContain("could not be resumed");
  });
});
