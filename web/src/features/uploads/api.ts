import { apiFetch, apiJson, type IngestEvent, readEventStream, type UploadResult } from "@/shared/api";

export function uploadDocuments(files: File[], topic: string): Promise<UploadResult[]> {
  const form = new FormData();
  for (const file of files) form.append("files", file);
  if (topic) form.append("topic", topic);
  return apiJson<UploadResult[]>("/ingest/upload", { method: "POST", form });
}

export async function* ingestProgress(jobId: string): AsyncGenerator<IngestEvent, void, void> {
  yield* readEventStream<IngestEvent>(await apiFetch(`/ingest/stream/${encodeURIComponent(jobId)}`));
}
