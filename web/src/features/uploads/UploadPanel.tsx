import { useMutation } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";
import { control, cx } from "@/shared/lib/classes";
import { uploadDocuments } from "./api";
import { useUploads } from "./store";

// The API is the authority (MAX_UPLOAD_FILES_PER_REQUEST, app/core/config.py): this only fails a too-large pick before the round trip.
const MAX_FILES = 5;
const tooMany = `pick at most ${MAX_FILES} files per upload`;

/** Builds the knowledge base. Unrelated to attaching an image to a message. */
export function UploadPanel() {
  const { lines, fail, track } = useUploads();
  const [topic, setTopic] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const upload = useMutation({
    mutationFn: (files: File[]) => uploadDocuments(files, topic.trim()),
    onSuccess: (results) => {
      if (input.current) input.current.value = "";
      setTopic("");
      for (const { filename, job_id, error } of results) {
        // A file the API refused has no job to follow, only its own reason.
        if (job_id) void track(filename, job_id);
        else fail(filename, error ?? "upload failed");
      }
    },
    onError: (error, files) => fail(files.map((file) => file.name).join(", "), error.message),
  });

  const pick = () => {
    const count = input.current?.files?.length ?? 0;
    if (count > MAX_FILES) {
      fail(`${count} files selected`, tooMany);
      if (input.current) input.current.value = "";
    }
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const files = [...(input.current?.files ?? [])];
    if (files.length === 0) return;
    if (files.length > MAX_FILES) fail(files.map((file) => file.name).join(", "), tooMany);
    else upload.mutate(files);
  };

  return (
    <details id="uploadPanel" className="mb-3 rounded-xl border border-line bg-elevated px-3 py-2.5 text-sm">
      <summary className="cursor-pointer text-ink-2 hover:text-ink">
        📄 Upload documents (PDF/DOCX) to the knowledge base, up to {MAX_FILES} at a time
      </summary>
      <form id="uploadForm" className="mt-2.5 flex flex-wrap gap-2" onSubmit={submit}>
        <input id="uploadFiles" ref={input} type="file" accept=".pdf,.docx" multiple aria-label="Documents" title={`Up to ${MAX_FILES} files per upload`} onChange={pick} />
        <input id="uploadTopic" className={cx(control, "min-w-32 flex-1")} placeholder="Optional topic" aria-label="Topic" value={topic} onChange={(event) => setTopic(event.target.value)} />
        <button type="submit" id="uploadSubmit" className="cursor-pointer rounded-lg bg-accent px-3.5 py-1.5 font-semibold text-accent-contrast enabled:hover:bg-accent-hover disabled:cursor-default disabled:opacity-50" disabled={upload.isPending}>
          Upload
        </button>
      </form>
      <div id="uploadStatus" className="mt-2.5 flex flex-col gap-1">
        {lines.map((line) => (
          <div key={line.id} className={cx("upload-status-line flex items-center gap-1.5 text-xs", line.state === "done" ? "upload-done text-success" : line.state === "error" ? "upload-error text-danger" : "text-muted")}>
            <span className="flex-1 truncate">{line.name}</span>
            {line.percent !== null && <progress className="h-1.5 w-20 accent-accent" max={100} value={line.percent} />}
            <span>{line.text}</span>
          </div>
        ))}
      </div>
    </details>
  );
}
