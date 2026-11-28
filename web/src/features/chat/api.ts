import { apiFetch, apiJson, type PendingApproval, type Schemas } from "@/shared/api";

type TurnRequest = Pick<Schemas["ChatRequest"], "message" | "thread_id" | "images">;

export const startTurn = (body: TurnRequest, signal: AbortSignal) =>
  apiFetch("/chat/stream/queued", { method: "POST", json: body, signal });

export const resumeTurn = (body: Schemas["ResumeRequest"], signal: AbortSignal) =>
  apiFetch("/chat/resume", { method: "POST", json: body, signal });

export const cancelTurn = (body: Schemas["CancelRequest"]) => apiFetch("/chat/cancel", { method: "POST", json: body });

/** A turn's events again, from after `lastEventId`: the turn runs on a worker whether or not anyone is reading. */
export const rereadTurn = (requestId: string, lastEventId: string | null, signal: AbortSignal) =>
  apiFetch(`/chat/stream/${encodeURIComponent(requestId)}`, { signal, lastEventId });

export const readPendingApproval = (threadId: string) =>
  apiJson<PendingApproval | null>(`/chat/sessions/${encodeURIComponent(threadId)}/pending_approval`);
