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

export const SUGGESTED_QUESTIONS: Record<string, string[]> = {
  ecorp: [
    "What is a LangGraph checkpointer?",
    "What's 15% of 2,450?",
    "Who works on Ecorp's engineering team?",
    "How does Qdrant's hybrid search work?",
    "What's (48 * 7) + 132?",
    "Is there a bundled skill for writing a report?",
  ],
  support: [
    "What are Ecorp's support hours?",
    "How do I reset my password?",
    "What's the status of ticket #4471?",
    "My dashboard won't load — can you open a ticket?",
    "What are all of my open tickets?",
    "Can you just refund my last order right now?",
    "The same issue happened again this morning — I already have a ticket open for it.",
    "Can you check this page for me — https://example.com — it seems to contradict what you told me?",
    "Here's the error log the customer pasted — can you run it through a script and tell me how many times 'db_timeout' shows up: db_timeout at 09:12, retry ok, db_timeout at 09:14, auth_failed at 09:15",
    "Check our API docs page (https://example.com) for the real rate limit, then verify it against what the customer says they're seeing (60/min).",
  ],
  ops: [
    "Did anything break overnight?",
    "What does the current error rate look like?",
    "Show me recent incidents.",
    "Is latency higher than usual right now?",
    "Are there any open incidents right now?",
    "Post a summary of current latency to the team channel.",
    "Has moderation blocked more requests than usual today?",
    "Check if our payment processor's status page (https://www.githubstatus.com/) shows an incident.",
    "Can you write and run a script to find the 95th percentile of these latencies: 120, 340, 90, 500, 210, 180, 620, 150?",
    "Check our payment processor's status page (https://www.githubstatus.com/), then compute how many incidents they've had this month from the page's own history — and check whether any of ours line up with theirs.",
    "Latency just spiked and our payment processor (https://www.githubstatus.com/) looks like a likely cause — is this our fault or theirs?",
    "Has litellm-proxy come up in our incident log before?",
  ],
  sales: [
    "A lead asked what Ecorp actually does.",
    "This lead wants pricing and to talk to someone directly.",
    "What follow-ups are pending right now?",
    "Draft a reply for a lead asking about our enterprise plan.",
    "This lead hasn't responded after three follow-ups — what should we do?",
    "Schedule a follow-up with this lead in 3 days.",
    "Give me a brief on this lead's history.",
    "New lead Jordan (jordan@example.com) says their company site is https://example.com — log this and research their company before I call them.",
    "This lead wants a 3-year deal at $50k/year with 5% annual escalation and a 10% volume discount on the total — what's the real contract value?",
    "Pull out any team-size or hiring signals from Jordan's company site research and summarize what that suggests about deal size.",
  ],
};
