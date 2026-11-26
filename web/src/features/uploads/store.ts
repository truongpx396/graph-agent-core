// One status line per file, each tracked on its own: a slow or failed file never hides the others.
import { create } from "zustand";
import type { IngestEvent } from "@/shared/api";
import { randomId } from "@/shared/lib/id";
import { ingestProgress } from "./api";

export interface UploadLine {
  id: string;
  name: string;
  state: "running" | "done" | "error";
  text: string;
  /** Null until the worker reports chunk counts: a bar with nothing to say is not shown. */
  percent: number | null;
}

export function lineAfter(line: UploadLine, event: IngestEvent): UploadLine {
  switch (event.type) {
    case "started":
      return { ...line, text: "processing…" };
    case "progress": {
      const percent = event.total > 0 ? Math.round((event.done / event.total) * 100) : 0;
      return { ...line, percent, text: `processing… ${percent}% (${event.done}/${event.total} chunks)` };
    }
    case "done":
      return { ...line, state: "done", percent: null, text: `✓ indexed (${event.chunks} chunk${event.chunks === 1 ? "" : "s"})` };
    case "error":
      return { ...line, state: "error", percent: null, text: `✕ ${event.content}` };
    default:
      return line;
  }
}

interface UploadState {
  lines: UploadLine[];
  fail: (name: string, reason: string) => void;
  track: (name: string, jobId: string) => Promise<void>;
}

export const useUploads = create<UploadState>((set) => {
  const add = (line: Omit<UploadLine, "id" | "percent">): string => {
    const id = randomId();
    set((state) => ({ lines: [...state.lines, { ...line, id, percent: null }] }));
    return id;
  };
  const change = (id: string, next: (line: UploadLine) => UploadLine) =>
    set((state) => ({ lines: state.lines.map((line) => (line.id === id ? next(line) : line)) }));

  return {
    lines: [],
    fail(name, reason) {
      add({ name, state: "error", text: `✕ ${reason}` });
    },
    async track(name, jobId) {
      const id = add({ name, state: "running", text: "uploading…" });
      try {
        for await (const event of ingestProgress(jobId)) change(id, (line) => lineAfter(line, event));
      } catch (error) {
        const reason = error instanceof Error ? error.message : String(error);
        change(id, (line) => ({ ...line, state: "error", percent: null, text: `✕ ${reason}` }));
      }
    },
  };
});
