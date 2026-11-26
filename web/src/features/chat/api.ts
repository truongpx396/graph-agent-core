import { apiFetch, type Schemas } from "@/shared/api";

type TurnRequest = Pick<Schemas["ChatRequest"], "message" | "thread_id" | "images">;

export const startTurn = (body: TurnRequest, signal: AbortSignal) =>
  apiFetch("/chat/stream/queued", { method: "POST", json: body, signal });

export const resumeTurn = (body: Schemas["ResumeRequest"], signal: AbortSignal) =>
  apiFetch("/chat/resume", { method: "POST", json: body, signal });

export const cancelTurn = (body: Schemas["CancelRequest"]) => apiFetch("/chat/cancel", { method: "POST", json: body });
