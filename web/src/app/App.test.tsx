// The features together, as a person uses them, against a fake API.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "@/features/auth";
import { useChat } from "@/features/chat";
import { deferred, fakeApi, json, openStream, sse } from "@/test/fakeApi";
import { App } from "./App";
import { createQueryClient } from "./queryClient";
import { wire } from "./wiring";

let unwire = () => {};
afterEach(() => unwire());

const session = { thread_id: "t-old", title: "An older chat", created_at: "2026-01-01T00:00:00Z", last_active_at: "2026-01-01T00:00:00Z" };
const message = (index: number, role: string, text: string) => ({ index, role, text });

function start(routes: Parameters<typeof fakeApi>[0] = {}) {
  const api = fakeApi({ "GET /chat/sessions": () => json([session]), ...routes });
  const queryClient = createQueryClient();
  unwire = wire(queryClient);
  void useAuth.getState().init();
  const view = render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  );
  const element = (selector: string) => view.container.querySelector(selector) as HTMLInputElement;
  return { api, user: userEvent.setup(), element, container: view.container };
}

describe("the page", () => {
  it("answers a typed message and refreshes the conversation list", async () => {
    const { api, user, element } = start({
      "POST /chat/stream/queued": () =>
        sse(
          { type: "tool_start", tool: "calculator", args: { expression: "21*2" } },
          { type: "tool_end", tool: "calculator" },
          { type: "token", content: "It is **42**." },
          { type: "citations", items: [{ marker: "[1]", doc_id: "d", title: "Support Hours", text: "", score: 1 }], ungrounded_claims_count: 0 },
          { type: "followups", items: ["And doubled?"] },
          { type: "done" },
        ),
    });
    await user.type(element("#input"), "what is 21 * 2?{Enter}");
    expect((await screen.findByText("42")).closest("strong")).not.toBeNull();
    expect(element(".tool-activity-line").textContent).toBe('✓ calculator({"expression":"21*2"})');
    expect(element(".citations .citation-item").textContent).toBe("[1]Support Hours");
    expect(screen.getByRole("button", { name: "And doubled?" })).toBeTruthy();
    expect(element("#send").disabled).toBe(false);
    await waitFor(() => expect(api.sent("GET /chat/sessions").length).toBeGreaterThan(1));
    expect(api.sent("POST /chat/stream/queued")[0]?.headers).toMatchObject({ "X-Tenant-Id": "ecorp", "X-Principal-Id": "web-user", "X-Domain": "ecorp" });
  });

  it("asks for approval and sends the decision", async () => {
    const { api, user, element } = start({
      "POST /chat/stream/queued": () => sse({ type: "approval_required", tool_calls: [{ name: "remember", args: { content: "coffee" } }] }),
      "POST /chat/resume": () => sse({ type: "token", content: "Noted." }, { type: "done" }),
    });
    await user.type(element("#input"), "remember coffee{Enter}");
    await screen.findByRole("button", { name: "Approve" });
    expect(element(".approval-question").textContent).toBe('⏸ Approve this action? remember({"content":"coffee"})');
    expect(element("#send").disabled).toBe(true);
    expect(element("#newChat").disabled).toBe(false);
    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(await screen.findByText("Noted.")).toBeTruthy();
    expect(api.sent("POST /chat/resume")[0]?.body).toMatchObject({ approved: false });
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("opens an older conversation, pages back through it and restores its pause", async () => {
    const { api, user, container } = start({
      "GET /chat/sessions/t-old/messages": (call) =>
        json(call.url.includes("before=50") ? [message(0, "user", "the first question")] : [message(50, "assistant", "a later answer")]),
      "GET /chat/sessions/t-old/pending_approval": () => json({ tool_calls: [{ name: "create_ticket", args: {} }], resumable: true }),
    });
    await user.click(await screen.findByRole("button", { name: "An older chat" }));
    expect(await screen.findByText("a later answer")).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Approve" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Load earlier messages" }));
    expect(await screen.findByText("the first question")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Load earlier messages" })).toBeNull();
    // Oldest history first, then what is live.
    expect([...container.querySelectorAll("#messages .msg")].map((node) => node.className.split(" ")[1])).toEqual(["user", "assistant", "assistant"]);
    expect(api.sent("GET /chat/sessions/t-old/messages").map((call) => call.url.split("?")[1])).toEqual(["limit=50", "limit=50&before=50"]);
  });

  it("follows a streaming answer, but not once the reader has scrolled up, until they send again", async () => {
    const stream = openStream();
    const { user, element } = start({ "POST /chat/stream/queued": () => stream.response });
    const box = element("#messages") as unknown as HTMLDivElement;
    let height = 1000;
    Object.defineProperties(box, { scrollHeight: { get: () => height }, clientHeight: { get: () => 200 } });
    await user.type(element("#input"), "hi{Enter}");
    stream.send({ type: "token", content: "one " });
    await waitFor(() => expect(box.scrollTop).toBe(1000));
    // No scroll event yet: the next token can arrive before the browser delivers it.
    box.scrollTop = 100;
    height = 1400;
    stream.send({ type: "token", content: "two" });
    await screen.findByText("two");
    expect(box.scrollTop).toBe(100);
    stream.send({ type: "done" });
    await waitFor(() => expect(element("#send").disabled).toBe(false));
    await user.type(element("#input"), "again{Enter}");
    await waitFor(() => expect(box.scrollTop).toBe(1400));
  });

  it("marks the open conversation in the list, and starts a new one from it", async () => {
    const { user, element, container } = start({
      "GET /chat/sessions/t-old/messages": () => json([message(0, "user", "an old question")]),
      "GET /chat/sessions/t-old/pending_approval": () => json(null),
    });
    const older = await screen.findByRole("button", { name: "An older chat" });
    const fresh = () => container.querySelector('#sessionList [aria-current="page"]')?.textContent;
    expect(fresh()).toBe("New conversation");
    await user.click(older);
    expect(await screen.findByText("an old question")).toBeTruthy();
    expect(older.getAttribute("aria-current")).toBe("page");
    expect(fresh()).toBe("An older chat");
    await user.click(element("#newChat"));
    expect(older.getAttribute("aria-current")).toBeNull();
    expect(container.querySelectorAll("#messages .msg")).toHaveLength(0);
  });

  it("does not switch conversations while a turn is streaming", async () => {
    const stream = openStream();
    const { user, element } = start({ "POST /chat/stream/queued": () => stream.response });
    const older = (await screen.findByRole("button", { name: "An older chat" })) as HTMLButtonElement;
    await user.type(element("#input"), "hi{Enter}");
    await waitFor(() => expect(older.disabled).toBe(true));
    expect(element("#newChat").disabled).toBe(true);
    stream.send({ type: "done" });
    stream.close();
    await waitFor(() => expect(older.disabled).toBe(false));
  });

  it("holds a restored pause back until its history is on screen", async () => {
    const page = deferred();
    const { user, container } = start({
      "GET /chat/sessions/t-old/messages": () => page.response,
      "GET /chat/sessions/t-old/pending_approval": () => json({ tool_calls: [{ name: "create_ticket", args: {} }], resumable: true }),
    });
    await user.click(await screen.findByRole("button", { name: "An older chat" }));
    await waitFor(() => expect(useChat.getState().turn?.phase).toBe("paused"));
    expect(container.querySelectorAll("#messages .msg")).toHaveLength(0);
    page.release(json([message(0, "user", "open a ticket")]));
    expect(await screen.findByText("open a ticket")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy();
  });

  it("starts a new conversation under the new domain, even while one is paused", async () => {
    const { api, user, element, container } = start({
      "POST /chat/stream/queued": () => sse({ type: "approval_required", tool_calls: [{ name: "remember", args: {} }] }),
    });
    await user.type(element("#input"), "hi{Enter}");
    await screen.findByRole("button", { name: "Approve" });
    const thread = useChat.getState().threadId;
    await user.selectOptions(element("#domainSelect"), "support");
    expect(useChat.getState().threadId).not.toBe(thread);
    expect(container.querySelectorAll("#messages .msg")).toHaveLength(0);
    expect(element("#send").disabled).toBe(false);
    await waitFor(() => expect(api.sent("GET /chat/sessions").at(-1)?.headers["X-Domain"]).toBe("support"));
    expect(screen.getByText("Tier-1 Support Copilot:")).toBeTruthy();
  });

  it.each(["tenant", "principal"])("starts a new conversation when the %s changes, since the old one is someone else's", async (field) => {
    // The server answers another caller's send on a thread with a 404.
    vi.spyOn(window, "prompt").mockReturnValue("someone-else");
    const { api, user, element, container } = start({ "POST /chat/stream/queued": () => sse({ type: "done" }) });
    await user.type(element("#input"), "hi{Enter}");
    await waitFor(() => expect(useChat.getState().turn).toBeNull());
    const thread = useChat.getState().threadId;
    await user.selectOptions(element(`#${field}`), "__add_new__");
    expect(useChat.getState().threadId).not.toBe(thread);
    expect(container.querySelectorAll("#messages .msg")).toHaveLength(0);
    const header = field === "tenant" ? "X-Tenant-Id" : "X-Principal-Id";
    await waitFor(() => expect(api.sent("GET /chat/sessions").at(-1)?.headers[header]).toBe("someone-else"));
  });

  it("follows each upload, and shows a refused file's own reason", async () => {
    const { user, element } = start({
      "POST /ingest/upload": () => json([{ filename: "a.pdf", job_id: "j1" }, { filename: "b.pdf", job_id: null, error: "not a PDF" }]),
      "GET /ingest/stream/j1": () => sse({ type: "started" }, { type: "done", chunks: 3 }),
    });
    await user.upload(element("#uploadFiles"), [new File(["a"], "a.pdf"), new File(["b"], "b.pdf")]);
    await user.click(element("#uploadSubmit"));
    expect(await screen.findByText("✓ indexed (3 chunks)")).toBeTruthy();
    expect(screen.getByText("✕ not a PDF")).toBeTruthy();
  });

  it("shows who to sign in as, not the pickers, in identity mode", async () => {
    document.head.innerHTML = `<meta name="agent-auth" content='{"mode":"identity","issuer":"https://a.test","audience":"x","scope":"s","client_id_prefix":"api-"}'>`;
    const { element, container } = start();
    expect(await screen.findByText("Sign in to chat.")).toBeTruthy();
    expect(element("#authOrg")).toBeTruthy();
    expect(container.querySelector("#tenant, #principal")).toBeNull();
  });
});
