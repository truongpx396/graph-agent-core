-- A person's thumbs up or down on one answer, with an optional comment (PRODUCTION_REVIEW.md R28; app/agent/runtime/feedback.py). Advisory: it feeds a counter
-- and a reviewer's reading, never a decision the agent makes. Scoped by tenant and person like every row about a conversation; a later rating of the same answer by the
-- same person REPLACES the earlier one (the primary key), so changing one's mind is not a second vote. `answer_ref` is `message:<index>` when the client knows the
-- answer's place in the transcript, else `request:<id>`.
--
-- Existing deployments: this runs on a NEW volume only. Apply it once to an existing one (idempotent):
--   docker compose ... exec -T postgres psql -U langfuse -d postgres -f /docker-entrypoint-initdb.d/26-chat-feedback.sql
\connect appdata

CREATE TABLE IF NOT EXISTS chat_feedback (
    tenant TEXT NOT NULL,
    principal TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    answer_ref TEXT NOT NULL,
    domain TEXT NOT NULL,
    request_id TEXT,
    rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant, principal, thread_id, answer_ref)
);

CREATE INDEX IF NOT EXISTS chat_feedback_tenant_created_idx ON chat_feedback (tenant, created_at DESC);
