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

// One entry per thing a domain can do, so no two buttons exercise the same tool, skill or subagent the same way. Each must make sense in a new
// conversation; the few that build on an earlier one (a ticket, an incident, a lead) come after the entry that creates it.
export const SUGGESTED_QUESTIONS: Record<string, string[]> = {
  ecorp: [
    "What is a LangGraph checkpointer?",
    "What's 15% of 2,450?",
    "Who works on Ecorp's engineering team?",
    "Prepare an onboarding brief for Priya Nair.",
    "Turn these into an expense report: taxi $23.50, hotel $180, team lunch $64.20",
    "Have a researcher pull together what our docs say about LangGraph and Qdrant, in three bullets.",
    "Add to the company knowledge base: our refund window is 30 days.",
    "Remember that I prefer short, bulleted answers.",
  ],
  support: [
    "What are Ecorp's support hours?",
    "How do I reset my password?",
    "My dashboard won't load — can you open a ticket?",
    "What are all of my open tickets?",
    "The same issue happened again this morning — I already have a ticket open for it.",
    "What's the latest on my most recent ticket?",
    "Have a researcher check my past tickets and the docs for anything about the dashboard not loading.",
    "Can you just refund my last order right now?",
    "Our whole team has been locked out since 9am — open an urgent ticket and escalate it to a human.",
    "It's not working.",
    "A customer says GitHub's API cuts them off after 60 requests an hour. Is that the real limit? Check https://api.github.com/rate_limit",
    "A customer sees ERR_CERT_AUTHORITY_INVALID in their browser — search the web for what causes it.",
    "Here's the error log a customer pasted — count how many times 'db_timeout' shows up: db_timeout at 09:12, retry ok, db_timeout at 09:14, auth_failed at 09:15",
  ],
  ops: [
    "Did anything break overnight?",
    "Give me a shift handover for the next on-call.",
    "Log an incident: checkout requests timed out for ten minutes around 09:40 and recovered after a worker restart.",
    "Are there any open incidents right now?",
    "The checkout timeouts are fixed, we added a second worker — resolve that incident.",
    "Has litellm-proxy come up in our incident log before?",
    "Post a summary of current latency to the team channel.",
    "Does GitHub's status page (https://www.githubstatus.com/) show an incident right now?",
    "Latency just spiked and GitHub (https://www.githubstatus.com/) looks like a likely cause — is this our fault or theirs?",
    "Search the web for Cloudflare's status page, open it, and tell me whether it reports an incident.",
    "Can you write and run a script to find the 95th percentile of these latencies: 120, 340, 90, 500, 210, 180, 620, 150?",
  ],
  sales: [
    "A lead asked what Ecorp actually does.",
    "New lead Jordan (jordan@example.com) says their company site is https://qdrant.tech — log this and research their company before I call them.",
    "Draft a reply to Jordan (jordan@example.com), who asked about our enterprise plan and pricing.",
    "Schedule a follow-up with jordan@example.com in 3 days to send pricing.",
    "What follow-ups are pending right now?",
    "Jordan (jordan@example.com) wants pricing and to talk to someone directly — hand them off to a rep.",
    "Package a lead brief on jordan@example.com before my call.",
    "jordan@example.com hasn't replied after three follow-ups — what should we do?",
    "New contact Sam (sam@example.org) says they've already bought from a competitor — record that and close them out.",
    "A lead wants a 3-year deal at $50k/year with 5% annual escalation and a 10% volume discount on the total — what's the real contract value?",
  ],
};
