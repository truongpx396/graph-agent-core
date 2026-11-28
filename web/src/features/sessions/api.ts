import { apiJson, type SessionMessage, type SessionSummary } from "@/shared/api";

/** A long conversation is read a page at a time, newest page first (docs/PRODUCTION_REVIEW.md R27). */
export const PAGE_SIZE = 50;

const path = (threadId: string) => `/chat/sessions/${encodeURIComponent(threadId)}` as const;

export const listSessions = () => apiJson<SessionSummary[]>("/chat/sessions");

export const readTranscript = (threadId: string, before?: number) =>
  apiJson<SessionMessage[]>(`${path(threadId)}/messages?limit=${PAGE_SIZE}${before === undefined ? "" : `&before=${before}`}`);
