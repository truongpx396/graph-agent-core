import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { bindRequestContext } from "@/shared/api";
import { fakeApi, json, openStream, sse } from "@/test/fakeApi";
import type { AssistantEntry } from "./entry";
import { useChat } from "./store";
import { RECONNECT_DELAYS } from "./stream";

const chat = () => useChat.getState();
const reply = () => chat().entries.findLast((entry) => entry.kind === "assistant") as AssistantEntry;
const pause = { type: "approval_required", tool_calls: [{ name: "remember", args: { content: "x" } }] };
const bound = { ...pause, approval_id: "apr_1", items: [{ call_id: "c1", tier: "mutating", title: "Remember a fact", effect: "Saves x", reversible: false }] };

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
    expect(reply().notice).toEqual({ tone: "failed", text: "Monthly budget reached." });
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

  it("says what was declined once the turn goes on without it", async () => {
    fakeApi({
      "POST /chat/stream/queued": () => sse(bound),
      "POST /chat/resume": () => sse({ type: "status", state: "queued" }, { type: "token", content: "Not saved." }, { type: "done" }),
    });
    await chat().send("remember x");
    await chat().decide(false);
    expect(reply().declined).toEqual(["Remember a fact"]);
  });

  it("claims nothing and shows the request again when the answer is refused", async () => {
    fakeApi({
      "POST /chat/stream/queued": () => sse(bound),
      "POST /chat/resume": () => json({ code: "rate_limited", message: "Too many requests." }, 429),
      "GET /chat/sessions/t-refused/pending_approval": () => json({ tool_calls: bound.tool_calls, resumable: true, approval_id: "apr_1", items: bound.items }),
    });
    useChat.setState({ threadId: "t-refused" });
    await chat().send("remember x");
    await chat().decide(false);
    const [answered, waiting] = chat().entries.filter((entry): entry is AssistantEntry => entry.kind === "assistant");
    expect(answered).toMatchObject({ declined: [], notice: { tone: "blocked" } });
    expect(waiting?.approval?.id).toBe("apr_1"); // the server is still waiting, so the card is back
    expect(chat().turn?.phase).toBe("paused");
  });

  it("stays paused when the resumed turn asks again", async () => {
    // The page this replaces finished the turn here: Stop disappeared while a second approval was on screen.
    fakeApi({ "POST /chat/stream/queued": () => sse(pause), "POST /chat/resume": () => sse(pause) });
    await chat().send("remember x");
    await chat().decide(false);
    expect(chat().turn?.phase).toBe("paused");
    expect(reply().approval?.calls).toHaveLength(1);
  });

  it("is cancelled by Stop through the cancel stream", async () => {
    fakeApi({
      "POST /chat/stream/queued": () => sse(pause),
      "POST /chat/cancel": () => sse({ type: "error", code: "cancelled", content: "cancelled" }),
    });
    await chat().send("remember x");
    await chat().stop();
    expect(reply()).toMatchObject({ approval: null, notice: { tone: "plain", text: "Stopped." } });
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
    expect(reply().notice?.text).toContain("can't be answered");
  });

  it("names the pause it was shown, also after a reload", async () => {
    const api = fakeApi({ "POST /chat/resume": () => sse({ type: "done" }) });
    chat().open("t1");
    chat().restorePendingApproval("t1", { tool_calls: bound.tool_calls, resumable: true, approval_id: "apr_9", items: [] });
    await chat().decide(true);
    expect(api.sent("POST /chat/resume")[0]?.body).toEqual({ thread_id: "t1", approved: true, approval_id: "apr_9" });
  });

  it("shows what is waiting now when the pause it answered had been replaced, and runs nothing", async () => {
    const api = fakeApi({
      "POST /chat/stream/queued": () => sse(bound),
      "POST /chat/resume": () => sse({ type: "error", code: "approval_superseded", content: "This request was replaced.", details: { kind: "lost" } }),
      "GET /chat/sessions/t-super/pending_approval": () => json({ tool_calls: bound.tool_calls, resumable: true, approval_id: "apr_2", items: bound.items }),
    });
    useChat.setState({ threadId: "t-super" });
    await chat().send("remember x");
    await chat().decide(false);
    await vi.waitFor(() => expect(chat().turn?.phase).toBe("paused"));
    const [answered, waiting] = chat().entries.filter((entry): entry is AssistantEntry => entry.kind === "assistant");
    expect(answered?.notice).toEqual({ tone: "lost", text: "This request was replaced. Nothing was decided." });
    expect(answered?.approval).toBeNull(); // no second action on the old request
    expect(answered?.declined).toEqual([]); // "Declined" beside "Nothing was decided" would contradict it
    expect(waiting?.approval?.id).toBe("apr_2");
    expect(api.sent("POST /chat/resume")).toHaveLength(1);
  });
});

const withId = (body: BodyInit | null) => new Response(body, { headers: { "X-Request-Id": "req-1" } });
const frames = (...events: [string, object][]) => events.map(([id, event]) => `id: ${id}\ndata: ${JSON.stringify(event)}\n\n`).join("");

/** Delivers its frames, then fails as a lost connection does. */
function drops(text: string) {
  let sent = false;
  return withId(
    new ReadableStream<Uint8Array>({
      pull(controller) {
        if (sent) controller.error(new TypeError("network error"));
        else controller.enqueue(new TextEncoder().encode(text));
        sent = true;
      },
    }),
  );
}

describe("a dropped stream", () => {
  afterEach(() => void vi.useRealTimers());

  it("is read again from the last event received, and the answer carries on", async () => {
    vi.useFakeTimers();
    const api = fakeApi({
      "POST /chat/stream/queued": () => drops(frames(["1-0", { type: "token", content: "one " }])),
      "GET /chat/stream/req-1": () => withId(frames(["2-0", { type: "token", content: "two" }], ["3-0", { type: "done" }])),
    });
    const turn = chat().send("count");
    await vi.advanceTimersByTimeAsync(10);
    expect(chat().reconnecting).toBe(true);
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS[0] ?? 0);
    await turn;
    expect(reply().answer).toBe("one two");
    expect(api.sent("GET /chat/stream/req-1")[0]?.headers["Last-Event-ID"]).toBe("1-0");
    expect(chat()).toMatchObject({ reconnecting: false, turn: null });
  });

  it("is given up on after the last attempt, with the partial answer marked as partial", async () => {
    vi.useFakeTimers();
    const api = fakeApi({
      "POST /chat/stream/queued": () => drops(frames(["1-0", { type: "token", content: "half" }])),
      "GET /chat/stream/req-1": () => withId(""),
    });
    const turn = chat().send("count");
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS.reduce((total, delay) => total + delay, 100));
    await turn;
    expect(api.sent("GET /chat/stream/req-1")).toHaveLength(RECONNECT_DELAYS.length);
    expect(reply()).toMatchObject({ answer: "half", notice: { tone: "failed" } });
    expect(chat()).toMatchObject({ reconnecting: false, turn: null });
  });

  it("is not retried when the server refuses it: that is an answer", async () => {
    const api = fakeApi({ "POST /chat/stream/queued": () => json({ code: "rate_limited", message: "Too many requests." }, 429) });
    await chat().send("hi");
    expect(api.calls).toHaveLength(1);
    expect(reply().notice).toEqual({ tone: "blocked", text: "Too many requests.", hint: "Wait a moment, then try again." });
  });
});
