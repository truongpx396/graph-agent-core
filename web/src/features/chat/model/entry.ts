import type { Citation, SessionMessage, ToolCall } from "@/shared/api";

export interface ToolActivity {
  tool: string;
  args: Record<string, unknown>;
  done: boolean;
}

export interface AssistantEntry {
  kind: "assistant";
  id: string;
  /** "Thinking", "Using x"... or null once there is nothing to wait for. */
  status: string | null;
  tools: ToolActivity[];
  /** Streamed and not yet confirmed: a `retry` throws away this and nothing else. */
  draft: string;
  /** The committed answer, markdown. */
  answer: string;
  /** A plain-text line from this app about the turn: "Stopped.", "[error: ...]". Never rendered as markdown. */
  notice: string | null;
  clarification: { question: string; options: string[] } | null;
  /** The answer restates a clarification already shown as buttons, so it is set back. */
  muted: boolean;
  approval: ToolCall[] | null;
  citations: Citation[];
  followups: string[];
}

export type Entry = AssistantEntry | { kind: "user" | "system"; id: string; text: string };

export function assistantEntry(id: string, over: Partial<AssistantEntry> = {}): AssistantEntry {
  return {
    kind: "assistant",
    id,
    status: "Thinking",
    tools: [],
    draft: "",
    answer: "",
    notice: null,
    clarification: null,
    muted: false,
    approval: null,
    citations: [],
    followups: [],
    ...over,
  };
}

/** A message replayed from `GET /chat/sessions/{id}/messages`: text only, no record of which tools ran. */
export function replayedEntry(message: SessionMessage): Entry {
  const id = `history-${message.index}`;
  if (message.role === "user" || message.role === "system") return { kind: message.role, id, text: message.text };
  return assistantEntry(id, { status: null, answer: message.text });
}
