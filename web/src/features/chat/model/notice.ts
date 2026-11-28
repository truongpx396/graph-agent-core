// What a refusal or failure says to a person: what happened and what to do (web/DESIGN.md, "Copy"). The server decides the KIND
// (`details.kind`); this only chooses words, keeping the server's own where they are meant for a person.
import type { Notice } from "./entry";

/** Replaces a server message written for an operator (it names request ids and workers). */
const WORDS: Record<string, string> = {
  worker_unreachable: "Nothing picked up your message. Try again in a moment.",
  worker_lost: "The work stopped before it finished. Send your message again.",
  checkpoint_lost: "There is nothing waiting for an answer any more.",
  checkpoint_incompatible: "This conversation can't be continued after an app update. Send your message again.",
  approval_superseded: "This request was replaced. Nothing was decided.",
  timeout: "That took too long. Try again.",
};

/** Who or what can change a refusal. */
const HINTS: Record<string, string> = {
  tenant_budget_exceeded: "A workspace admin can raise the limit.",
  personal_budget_exceeded: "A workspace admin can raise your limit.",
  insufficient_credits: "A workspace admin can add credit.",
  provider_budget_exceeded: "A workspace admin can raise the provider's limit.",
  cost_ceiling_exceeded: "Ask for less at once, or ask a workspace admin to raise the limit.",
  model_unpriced: "A workspace admin has to set a price for this model.",
  forbidden: "A workspace admin can give you access.",
  unauthenticated: "Sign in again.",
  rate_limited: "Wait a moment, then try again.",
  thread_busy: "Wait for the other turn to finish.",
  pending_approval: "Answer the request first.",
  moderation_blocked: "Reword your message.",
};

/** The front door's refusals carry no kind: they arrive as an HTTP status. */
const BLOCKED_STATUSES = new Set([401, 403, 429]);

export interface Failure {
  message: string;
  code?: string | null;
  kind?: string | null;
  status?: number;
}

/** `partial`: some of an answer is on screen, so the notice must say it is not the whole of it. */
export function noticeFor(failure: Failure, partial = false): Notice {
  const code = failure.code ?? "";
  if (code === "cancelled") return { tone: "plain", text: partial ? "Stopped before finishing." : "Stopped." };
  const tone: Notice["tone"] =
    failure.kind === "blocked" || failure.kind === "lost" ? failure.kind : BLOCKED_STATUSES.has(failure.status ?? 0) ? "blocked" : "failed";
  const words = WORDS[code] ?? failure.message;
  const text = partial ? `Not finished. ${words}` : words;
  const hint = HINTS[code];
  return hint ? { tone, text, hint } : { tone, text };
}
