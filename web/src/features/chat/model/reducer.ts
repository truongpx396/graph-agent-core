// What one stream event does to the assistant message it belongs to. Pure, so the whole event vocabulary is tested without a browser.
import type { ChatEvent } from "@/shared/api";
import type { AssistantEntry } from "./entry";

/**
 * Commits the draft. It APPENDS: text streamed before an approval pause is committed at the pause, and the answer that follows the
 * resume has to join it. (The page this replaces committed once per message, which left that second part styled as a draft for good.)
 */
export function promote(entry: AssistantEntry): AssistantEntry {
  if (!entry.draft) return entry;
  return { ...entry, answer: entry.answer ? `${entry.answer}\n\n${entry.draft}` : entry.draft, draft: "" };
}

/** The turn ended without a final answer: say so, and never let a partial answer pass for a whole one. */
export function interrupted(entry: AssistantEntry, message: string, cancelled = false): AssistantEntry {
  const settled = { ...promote(entry), muted: false, approval: null };
  if (settled.answer) return { ...settled, status: cancelled ? "Stopped before finishing." : `Interrupted: ${message}` };
  return { ...settled, status: null, notice: cancelled ? "Stopped." : `[error: ${message}]` };
}

function clarificationFrom(args: Record<string, unknown>): AssistantEntry["clarification"] {
  const { question, options } = args;
  if (!Array.isArray(options)) return null;
  return { question: typeof question === "string" ? question : "", options: options.map(String) };
}

function finishTool(tools: AssistantEntry["tools"], tool: string): AssistantEntry["tools"] {
  const index = tools.findLastIndex((item) => item.tool === tool && !item.done);
  return index === -1 ? tools : tools.map((item, i) => (i === index ? { ...item, done: true } : item));
}

export function applyEvent(entry: AssistantEntry, event: ChatEvent): AssistantEntry {
  switch (event.type) {
    case "token":
      return { ...entry, status: null, draft: entry.draft + event.content };
    case "retry":
      return { ...entry, status: "Revising answer", draft: "" };
    case "compacted":
      return { ...entry, status: "Summarizing older messages" };
    case "tool_start": {
      const clarification = event.tool === "ask_clarification" ? clarificationFrom(event.args) : null;
      return {
        ...entry,
        status: `Using ${event.tool}`,
        tools: [...entry.tools, { tool: event.tool, args: event.args, done: false }],
        clarification: clarification ?? entry.clarification,
        muted: entry.muted || clarification !== null,
      };
    }
    case "tool_end":
      return { ...entry, status: "Thinking", tools: finishTool(entry.tools, event.tool) };
    case "citations":
      return { ...promote(entry), citations: event.items };
    case "followups":
      return { ...promote(entry), followups: event.items };
    case "approval_required":
      return { ...promote(entry), status: null, approval: event.tool_calls };
    case "done":
      return { ...promote(entry), status: null, approval: null };
    case "error":
      return interrupted(entry, event.content, event.code === "cancelled");
    case "system_note":
      // About the conversation, not this answer: the store adds it as a line of its own.
      return entry;
    default:
      // A newer server may send a type this build does not know. Ignoring it beats failing the turn.
      return entry;
  }
}
