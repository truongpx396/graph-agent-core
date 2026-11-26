// The live conversation: what has been said since it was opened, and the one turn that may be in flight. Replayed history is server
// state and stays with TanStack Query (features/sessions).
import { create } from "zustand";
import { type ChatEvent, type PendingApproval, readEventStream, type ToolCall } from "@/shared/api";
import { randomId } from "@/shared/lib/id";
import { cancelTurn, resumeTurn, startTurn } from "../api";
import { type AssistantEntry, assistantEntry, type Entry } from "./entry";
import { applyEvent, interrupted, promote } from "./reducer";

export interface Attachment {
  /** What is sent: a URL or a data URI. */
  uri: string;
  /** What is shown: a data URI can be megabytes. */
  label: string;
}

export interface Turn {
  id: string;
  entryId: string;
  /** `paused` waits for a person at an approval: nothing is in flight, and they may leave and come back. */
  phase: "streaming" | "paused";
  abort: AbortController;
}

interface ChatState {
  threadId: string;
  /** Set when this conversation was opened from the list, so its history is read. */
  historyThreadId: string | null;
  entries: Entry[];
  turn: Turn | null;
  send: (text: string, attachment?: Attachment | null) => Promise<void>;
  decide: (approved: boolean) => Promise<void>;
  stop: () => Promise<void>;
  note: (text: string) => void;
  startFresh: () => void;
  open: (threadId: string) => void;
  restorePendingApproval: (threadId: string, pending: PendingApproval) => void;
}

const messageOf = (error: unknown) => (error instanceof Error ? error.message : String(error));

export const useChat = create<ChatState>((set, get) => {
  /** False once the person has left that conversation or its turn has ended: whatever still arrives for it is dropped. */
  const isCurrent = (turnId: string) => get().turn?.id === turnId;

  function update(entryId: string, change: (entry: AssistantEntry) => AssistantEntry): void {
    set((state) => ({
      entries: state.entries.map((entry) => (entry.id === entryId && entry.kind === "assistant" ? change(entry) : entry)),
    }));
  }

  function dispatch(turn: Turn, event: ChatEvent): void {
    if (event.type === "system_note") {
      get().note(event.content);
      return;
    }
    update(turn.entryId, (entry) => applyEvent(entry, event));
    if (event.type === "approval_required") set({ turn: { ...turn, phase: "paused" } });
    if (event.type === "done" || event.type === "error") set({ turn: null });
  }

  /** Feeds one response's stream into the turn. A stream that just ends, without a pause or a terminal event, still releases the page. */
  async function consume(turnId: string, request: Promise<Response>, onFailure: (message: string) => void): Promise<void> {
    try {
      for await (const event of readEventStream<ChatEvent>(await request)) {
        const turn = get().turn;
        if (turn?.id !== turnId) return;
        dispatch(turn, event);
      }
      const turn = get().turn;
      if (turn?.id !== turnId || turn.phase === "paused") return;
      update(turn.entryId, (entry) => ({ ...promote(entry), status: null }));
      set({ turn: null });
    } catch (error) {
      if (!isCurrent(turnId)) return;
      onFailure(messageOf(error));
      set({ turn: null });
    }
  }

  function leave(next: { threadId: string; historyThreadId: string | null }): void {
    // Stops reading, not the turn: the worker finishes it and it appears in the history.
    get().turn?.abort.abort();
    set({ ...next, entries: [], turn: null });
  }

  return {
    threadId: randomId(),
    historyThreadId: null,
    entries: [],
    turn: null,

    async send(text, attachment) {
      if (get().turn) return; // one turn at a time
      const entryId = randomId();
      const turn: Turn = { id: randomId(), entryId, phase: "streaming", abort: new AbortController() };
      const shown = attachment ? `${text}\n[attached image: ${attachment.label}]` : text;
      set((state) => ({
        turn,
        entries: [...state.entries, { kind: "user", id: randomId(), text: shown }, assistantEntry(entryId)],
      }));
      const body = { message: text, thread_id: get().threadId, images: attachment ? [attachment.uri] : [] };
      await consume(turn.id, startTurn(body, turn.abort.signal), (message) =>
        update(entryId, (entry) => interrupted(entry, message)),
      );
    },

    async decide(approved) {
      const turn = get().turn;
      if (turn?.phase !== "paused") return;
      // Both changes happen before any await, so a double click cannot send two decisions for one pause.
      set({ turn: { ...turn, phase: "streaming" } });
      update(turn.entryId, (entry) => ({ ...entry, approval: null, status: approved ? "Resuming" : "Cancelling that action" }));
      await consume(turn.id, resumeTurn({ thread_id: get().threadId, approved }, turn.abort.signal), (message) =>
        update(turn.entryId, (entry) => ({ ...entry, status: null, notice: `[resume failed: ${message}]` })),
      );
    },

    async stop() {
      const turn = get().turn;
      if (!turn) return;
      try {
        const response = await cancelTurn({ thread_id: get().threadId });
        if (turn.phase === "paused") {
          // Nothing is in flight, so this response is the outcome. A refused cancel leaves the approval as it was.
          for await (const event of readEventStream<ChatEvent>(response)) {
            const current = get().turn;
            if (current?.id !== turn.id) return;
            dispatch(current, event);
          }
        } else {
          // The stream already being read reports the cancellation; this one only confirms it.
          await response.body?.cancel();
        }
      } catch {
        // Best effort: whether the stop took effect is what the turn's own stream says.
      }
    },

    note(text) {
      set((state) => ({ entries: [...state.entries, { kind: "system", id: randomId(), text }] }));
    },

    startFresh: () => leave({ threadId: randomId(), historyThreadId: null }),
    open: (threadId) => leave({ threadId, historyThreadId: threadId }),

    restorePendingApproval(threadId, pending) {
      if (get().threadId !== threadId || get().turn) return;
      const entryId = randomId();
      if (!pending.resumable) {
        // The checkpoint's schema no longer matches this build: approve and reject would both be refused. The next message clears it.
        const notice = "A pending approval on this conversation could not be resumed after an app update.";
        set((state) => ({ entries: [...state.entries, assistantEntry(entryId, { status: null, notice })] }));
        return;
      }
      const approval = pending.tool_calls as unknown as ToolCall[];
      set((state) => ({
        entries: [...state.entries, assistantEntry(entryId, { status: null, approval })],
        turn: { id: randomId(), entryId, phase: "paused", abort: new AbortController() },
      }));
    },
  };
});
