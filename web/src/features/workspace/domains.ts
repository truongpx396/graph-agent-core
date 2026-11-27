import suggestions from "./suggestions.json";

// What each domain's agent is and can do, from its manifest and tool list (app/domains/*/domain.py), and example questions that exercise
// its real tools. A domain added to app/domains/registry.py needs a line here to be reachable from this page; the other two are optional.
export const DOMAINS = ["ecorp", "support", "ops", "sales"] as const;

export const DOMAIN_INFO: Record<string, { title: string; description: string }> = {
  ecorp: {
    title: "General Assistant",
    description:
      "Answers from Ecorp's knowledge base and general knowledge (with citations), does math, and looks up Ecorp staff. Can search for and follow a bundled skill's playbook, delegate a self-contained lookup to a subagent, and remember facts across conversations (memory writes need your approval first).",
  },
  support: {
    title: "Tier-1 Support Copilot",
    description:
      "Answers from the knowledge base and manages support tickets — opens one, checks status, adds a follow-up comment, lists yours. Can read a customer-linked third-party page live (never adds it to the knowledge base). Can't process a refund or change your account itself (no tool for that, by design) — escalates those to a human agent instead of guessing.",
  },
  ops: {
    title: "Internal Ops Assistant",
    description:
      "Reads this app's own live operational metrics (turn error rate, latency, tool errors, moderation blocks, rate limiting) and flags anything past its alert threshold. Can log/list/resolve durable incidents, check a vendor's live status page, and — for real computation calculator's plain arithmetic can't do — run a short script inside an isolated sandbox. Posting to the team channel and every sandbox/status-page action need your explicit approval every time — none of them ever run on its own initiative.",
  },
  sales: {
    title: "Sales / CRM Concierge",
    description:
      "Logs lead interactions, schedules and tracks follow-ups, packages a brief on a lead's history, researches a lead's company website live, and hands a hot lead to a human rep — or marks one lost. Has no tool that sends anything to a lead: every reply it drafts is for a human to review and send.",
  },
};

/** An example question and what it is meant to exercise, so a person can check the agent against it. */
export interface Suggestion {
  text: string;
  /** The tier the router sends it to when the router is on. `make route-buttons` checks every mark against the real classifier. */
  route?: "medium" | "strong";
  skill?: string;
  subagents?: string[];
  /** Two or more tool calls the turn should need; a name twice means two calls. */
  tools?: string[];
}

// In suggestions.json so that tests/domains/test_suggested_questions.py can hold every skill, subagent and tool it names against the domains.
// One entry per thing a domain can do, plus a few that exist to show routing. Each must make sense in a new conversation; the few that build on an
// earlier one (a ticket, an incident, a lead) come after the entry that creates it.
export const SUGGESTED_QUESTIONS = suggestions as Record<string, Suggestion[]>;
