import type { ApprovalItem, Citation, SessionMessage, Tier, ToolCall } from "@/shared/api";

/** One tool call of a turn, in the server's words. */
export interface Step {
  /** Pairs its end with its start, also between parallel calls of one tool. */
  callId: string;
  /** Never shown: the person reads `label`. */
  tool: string;
  label: string;
  tier: Tier | null;
  /** The specialist that made the call, when one did. */
  subagent: string | null;
  outcome: "running" | "ok" | "error" | "refused";
  durationMs: number | null;
  summary: string | null;
}

/** A pause: what it asks about, and the id an answer to it names. */
export interface Approval {
  id: string | null;
  calls: ToolCall[];
  /** One per call, in order. Empty for a pause an older server recorded. */
  items: ApprovalItem[];
}

export interface Usage {
  model: string | null;
  route: string | null;
  /** Null when unknown, never zero. */
  tokens: number | null;
  costUsd: number | null;
  calls: number;
}

/** A line from this app about the turn. `blocked`: a rule said no. `failed`: something broke. `lost`: what was waited for is gone. */
export interface Notice {
  tone: "plain" | "blocked" | "failed" | "lost";
  text: string;
  /** Who or what can change it. */
  hint?: string;
}

export interface AssistantEntry {
  kind: "assistant";
  id: string;
  /** What is happening now, or null once there is nothing to wait for. */
  status: string | null;
  /** A worker has the job. Until then the turn may be waiting for one. */
  started: boolean;
  /** When the wait for a worker began (epoch ms), for "Waiting to start". */
  since: number | null;
  steps: Step[];
  /** The turn is over: the steps fold away. */
  done: boolean;
  /** Time spent working, not counting a wait for the person. */
  workedMs: number;
  /** Streamed and not yet confirmed: a `retry` throws away this and nothing else. */
  draft: string;
  /** The committed answer, markdown. */
  answer: string;
  notice: Notice | null;
  clarification: { question: string; options: string[] } | null;
  /** The answer restates a clarification already shown as buttons, so it is set back. */
  muted: boolean;
  approval: Approval | null;
  /** Titles of the calls the person declined. */
  declined: string[];
  usage: Usage | null;
  citations: Citation[];
  followups: string[];
}

export type Entry = AssistantEntry | { kind: "user" | "system"; id: string; text: string };

export function assistantEntry(id: string, over: Partial<AssistantEntry> = {}): AssistantEntry {
  return {
    kind: "assistant",
    id,
    status: "Working",
    started: false,
    since: null,
    steps: [],
    done: false,
    workedMs: 0,
    draft: "",
    answer: "",
    notice: null,
    clarification: null,
    muted: false,
    approval: null,
    declined: [],
    usage: null,
    citations: [],
    followups: [],
    ...over,
  };
}

/** A message replayed from `GET /chat/sessions/{id}/messages`: text only, no record of which tools ran. */
export function replayedEntry(message: SessionMessage): Entry {
  const id = `history-${message.index}`;
  if (message.role === "user" || message.role === "system") return { kind: message.role, id, text: message.text };
  return assistantEntry(id, { status: null, started: true, done: true, answer: message.text });
}
