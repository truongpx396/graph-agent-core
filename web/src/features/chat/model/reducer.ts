// What one stream event does to the assistant message it belongs to. Pure, so the whole event vocabulary is tested without a browser.
import type { ChatEvent } from "@/shared/api";
import type { AssistantEntry, Notice, Step } from "./entry";
import { noticeFor } from "./notice";

/**
 * Commits the draft. It APPENDS: text streamed before an approval pause is committed at the pause, and the answer that follows the
 * resume has to join it. (The page this replaces committed once per message, which left that second part styled as a draft for good.)
 */
export function promote(entry: AssistantEntry): AssistantEntry {
  if (!entry.draft) return entry;
  return { ...entry, answer: entry.answer ? `${entry.answer}\n\n${entry.draft}` : entry.draft, draft: "" };
}

/** The turn ended without a final answer: say so, and never let a partial answer pass for a whole one. */
export function interrupted(entry: AssistantEntry, notice: Notice): AssistantEntry {
  // A step still running when the turn broke off did not finish: it must not go on animating.
  const steps = entry.steps.map((step) => (step.outcome === "running" ? { ...step, outcome: "error" as const } : step));
  return { ...promote(entry), steps, muted: false, approval: null, status: null, done: true, notice };
}

function clarificationFrom(args: Record<string, unknown>): AssistantEntry["clarification"] {
  const { question, options } = args;
  if (!Array.isArray(options)) return null;
  return { question: typeof question === "string" ? question : "", options: options.map(String) };
}

type Of<T extends ChatEvent["type"]> = Extract<ChatEvent, { type: T }>;

function stepFrom(event: Of<"tool_start">, position: number): Step {
  return {
    callId: event.call_id ?? `${event.tool}-${position}`,
    tool: event.tool,
    // The server names the step. Without a name (an older server) the person is told nothing they could not read.
    label: event.label ?? "A step",
    tier: event.tier ?? null,
    subagent: event.subagent ?? null,
    outcome: "running",
    durationMs: null,
    summary: null,
  };
}

function finish(steps: Step[], event: Of<"tool_end">): Step[] {
  // By call id; an older server sends none, so then the last running call of that tool.
  const index = event.call_id
    ? steps.findIndex((step) => step.callId === event.call_id)
    : steps.findLastIndex((step) => step.tool === event.tool && step.outcome === "running");
  const outcome = event.outcome === "error" || event.outcome === "refused" ? event.outcome : "ok";
  return steps.map((step, i) => (i === index ? { ...step, outcome, durationMs: event.duration_ms ?? null, summary: event.summary ?? null } : step));
}

function usageFrom(event: Of<"usage">): AssistantEntry["usage"] {
  const { input_tokens: input, output_tokens: output } = event;
  return {
    model: event.model,
    route: event.route,
    tokens: input === null || output === null ? null : input + output,
    costUsd: event.cost_usd,
    calls: event.model_calls,
  };
}

export function applyEvent(entry: AssistantEntry, event: ChatEvent): AssistantEntry {
  // Anything a worker wrote means a worker has the job. "queued" is the API's own.
  const working = event.type === "status" && event.state !== "started" ? entry : { ...entry, started: true };
  switch (event.type) {
    case "status":
      return event.state === "started" && working.status !== null ? { ...working, status: "Working" } : working;
    case "token":
      return { ...working, status: null, draft: working.draft + event.content };
    case "retry":
      return { ...working, status: "Rewriting the answer…", draft: "" };
    case "compacted":
      return { ...working, status: "Summarizing older messages" };
    case "tool_start": {
      const clarification = event.tool === "ask_clarification" ? clarificationFrom(event.args) : null;
      return {
        ...working,
        status: "Working",
        steps: [...working.steps, stepFrom(event, working.steps.length)],
        clarification: clarification ?? working.clarification,
        muted: working.muted || clarification !== null,
      };
    }
    case "tool_end":
      return { ...working, status: "Working", steps: finish(working.steps, event) };
    case "citations":
      return { ...promote(working), citations: event.items };
    case "followups":
      return { ...promote(working), followups: event.items };
    case "usage":
      return { ...working, usage: usageFrom(event) };
    case "approval_required":
      return {
        ...promote(working),
        status: null,
        approval: { id: event.approval_id ?? null, calls: event.tool_calls, items: event.items ?? [] },
      };
    case "done":
      return { ...promote(working), status: null, approval: null, done: true };
    case "error": {
      const notice = noticeFor(
        { message: event.content, code: event.code ?? null, kind: typeof event.details?.["kind"] === "string" ? event.details["kind"] : null },
        Boolean(working.answer || working.draft),
      );
      return interrupted(working, notice);
    }
    case "system_note":
      // About the conversation, not this answer: the store adds it as a line of its own.
      return entry;
    default:
      // A newer server may send a type this build does not know. Ignoring it beats failing the turn.
      return entry;
  }
}
