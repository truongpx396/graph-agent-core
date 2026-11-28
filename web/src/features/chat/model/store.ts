// The live conversation: what has been said since it was opened, and the one turn that may be in flight. Replayed history is server
// state and stays with TanStack Query (features/sessions).
import { create } from "zustand";
import { ApiError, type ChatEvent, type PendingApproval, readEventStream, type ToolCall } from "@/shared/api";
import { randomId } from "@/shared/lib/id";
import { cancelTurn, readPendingApproval, resumeTurn, startTurn } from "../api";
import { type AssistantEntry, assistantEntry, type Entry } from "./entry";
import { noticeFor } from "./notice";
import { applyEvent, interrupted, promote } from "./reducer";
import { follow } from "./stream";

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
  /** When the current stretch of work began, for the time a finished turn reports. */
  since: number;
  abort: AbortController;
  /** A decision sent and not yet seen to take effect: until the graph goes on, nothing may say it did. */
  unconfirmed: { declined: string[] } | null;
}

interface ChatState {
  threadId: string;
  /** Set when this conversation was opened from the list, so its history is read. */
  historyThreadId: string | null;
  entries: Entry[];
  turn: Turn | null;
  /** The turn's stream dropped and is being read again. */
  reconnecting: boolean;
  send: (text: string, attachment?: Attachment | null) => Promise<void>;
  decide: (approved: boolean) => Promise<void>;
  stop: () => Promise<void>;
  note: (text: string) => void;
  startFresh: () => void;
  open: (threadId: string) => void;
  restorePendingApproval: (threadId: string, pending: PendingApproval) => void;
}

/** Errors after which the server may hold a pause this page is not showing: read what is waiting now. */
const READ_THE_PAUSE_AGAIN = new Set(["approval_superseded", "pending_approval"]);

const failureOf = (error: unknown) =>
  error instanceof ApiError
    ? { message: error.message, code: error.code, kind: error.kind, status: error.status }
    : { message: error instanceof Error ? error.message : String(error) };

/** Adds the stretch of work that just ended: a wait for the person is not work. */
const worked = (turn: Turn) => (entry: AssistantEntry) => ({ ...entry, workedMs: entry.workedMs + (Date.now() - turn.since) });

const begin = (entryId: string, phase: Turn["phase"] = "streaming"): Turn => ({
  id: randomId(),
  entryId,
  phase,
  since: Date.now(),
  abort: new AbortController(),
  unconfirmed: null,
});

export const useChat = create<ChatState>((set, get) => {
  /** False once the person has left that conversation or its turn has ended: whatever still arrives for it is dropped. */
  const isCurrent = (turnId: string) => get().turn?.id === turnId;

  function update(entryId: string, change: (entry: AssistantEntry) => AssistantEntry): void {
    set((state) => ({
      entries: state.entries.map((entry) => (entry.id === entryId && entry.kind === "assistant" ? change(entry) : entry)),
    }));
  }

  /** A pause the server holds and this page no longer shows, or shows an older one of. */
  async function readPause(threadId: string): Promise<void> {
    const pending = await readPendingApproval(threadId).catch(() => null);
    if (pending) get().restorePendingApproval(threadId, pending);
  }

  function dispatch(received: Turn, event: ChatEvent): void {
    if (event.type === "system_note") {
      get().note(event.content);
      return;
    }
    let turn = received;
    if (turn.unconfirmed && event.type !== "status" && event.type !== "error") {
      const { declined } = turn.unconfirmed;
      update(turn.entryId, (entry) => ({ ...entry, declined: [...entry.declined, ...declined] }));
      turn = { ...turn, unconfirmed: null };
      set({ turn });
    }
    update(turn.entryId, (entry) => applyEvent(entry, event));
    if (event.type === "approval_required") {
      update(turn.entryId, worked(turn));
      set({ turn: { ...turn, phase: "paused" } });
    }
    if (event.type === "done" || event.type === "error") {
      if (turn.phase === "streaming") update(turn.entryId, worked(turn));
      set({ turn: null, reconnecting: false });
    }
    // An answer that ended in an error before the graph went on may still be waiting for one.
    if (event.type === "error" && (turn.unconfirmed || READ_THE_PAUSE_AGAIN.has(event.code ?? ""))) void readPause(get().threadId);
  }

  /** Feeds one request's stream into the turn. A stream that just ends, without a pause or a terminal event, still releases the page. */
  async function consume(turn: Turn, request: Promise<Response>): Promise<void> {
    try {
      await follow(request, {
        signal: turn.abort.signal,
        wanted: () => isCurrent(turn.id) && get().turn?.phase === "streaming",
        onEvent: (event) => {
          const current = get().turn;
          if (current) dispatch(current, event);
        },
        onReconnecting: (reconnecting) => set({ reconnecting }),
      });
      const current = get().turn;
      if (current?.id !== turn.id || current.phase === "paused") return;
      update(turn.entryId, (entry) => ({ ...promote(entry), status: null, done: true }));
      update(turn.entryId, worked(current));
      set({ turn: null, reconnecting: false });
      if (current.unconfirmed) await readPause(get().threadId);
    } catch (error) {
      if (!isCurrent(turn.id)) return;
      const unanswered = get().turn?.unconfirmed;
      const failure = failureOf(error);
      update(turn.entryId, (entry) => interrupted(entry, noticeFor(failure, Boolean(entry.answer || entry.draft))));
      set({ turn: null, reconnecting: false });
      if (unanswered || READ_THE_PAUSE_AGAIN.has(failure.code ?? "")) await readPause(get().threadId);
    }
  }

  function leave(next: { threadId: string; historyThreadId: string | null }): void {
    // Stops reading, not the turn: the worker finishes it and it appears in the history.
    get().turn?.abort.abort();
    set({ ...next, entries: [], turn: null, reconnecting: false });
  }

  return {
    threadId: randomId(),
    historyThreadId: null,
    entries: [],
    turn: null,
    reconnecting: false,

    async send(text, attachment) {
      if (get().turn) return; // one turn at a time
      const entryId = randomId();
      const turn = begin(entryId);
      const shown = attachment ? `${text}\n[attached image: ${attachment.label}]` : text;
      set((state) => ({
        turn,
        entries: [...state.entries, { kind: "user", id: randomId(), text: shown }, assistantEntry(entryId, { since: turn.since })],
      }));
      const body = { message: text, thread_id: get().threadId, images: attachment ? [attachment.uri] : [] };
      await consume(turn, startTurn(body, turn.abort.signal));
    },

    async decide(approved) {
      const paused = get().turn;
      if (paused?.phase !== "paused") return;
      const entry = get().entries.find((item) => item.id === paused.entryId);
      const approval = entry?.kind === "assistant" ? entry.approval : null;
      const titles = approval?.items.map((item) => item.title) ?? [];
      const declined = approved ? [] : titles.length > 0 ? titles : ["the request"];
      const turn: Turn = { ...paused, phase: "streaming", since: Date.now(), unconfirmed: { declined } };
      // Both changes happen before any await, so a double click cannot send two decisions for one pause.
      set({ turn });
      update(turn.entryId, (current) => ({ ...current, approval: null, status: approved ? "Working" : "Declining", started: false, since: turn.since }));
      // The id of the pause the person was shown: if another is waiting by now, nothing runs.
      const body = { thread_id: get().threadId, approved, ...(approval?.id ? { approval_id: approval.id } : {}) };
      await consume(turn, resumeTurn(body, turn.abort.signal));
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
      const shown = { status: null, started: true } as const;
      if (!pending.resumable) {
        // The checkpoint's schema no longer matches this build: approve and decline would both be refused. The next message clears it.
        const notice = { tone: "lost", text: "A request in this conversation can't be answered after an app update." } as const;
        set((state) => ({ entries: [...state.entries, assistantEntry(entryId, { ...shown, done: true, notice })] }));
        return;
      }
      const approval = {
        id: pending.approval_id ?? null,
        calls: pending.tool_calls as unknown as ToolCall[],
        items: pending.items ?? [],
      };
      set((state) => ({
        entries: [...state.entries, assistantEntry(entryId, { ...shown, approval })],
        turn: begin(entryId, "paused"),
      }));
    },
  };
});

/** The pause waiting for the person, if any. */
export function pendingApproval(state: ChatState): AssistantEntry["approval"] {
  if (state.turn?.phase !== "paused") return null;
  const entry = state.entries.find((item) => item.id === state.turn?.entryId);
  return entry?.kind === "assistant" ? entry.approval : null;
}
