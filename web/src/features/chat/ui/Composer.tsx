import { type DragEvent, type FormEvent, useEffect, useRef, useState } from "react";
import { button, control, cx } from "@/shared/lib/classes";
import { useChat } from "../model/store";
import { useAttachment } from "./useAttachment";

export function Composer() {
  const busy = useChat((state) => state.turn !== null);
  const send = useChat((state) => state.send);
  const stop = useChat((state) => state.stop);
  const [text, setText] = useState("");
  const [dragging, setDragging] = useState(false);
  const image = useAttachment();
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!busy) input.current?.focus();
  }, [busy]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const message = text.trim();
    if (!message || busy) return;
    void send(message, image.attachment);
    setText("");
    image.clear();
  };
  const drop = (event: DragEvent) => {
    event.preventDefault();
    setDragging(false);
    image.pickFile(event.dataTransfer.files[0]);
  };

  return (
    <form id="form" className="mt-1 flex flex-wrap gap-2 border-t border-line pt-3.5" onSubmit={submit}>
      {/* Above the input, where the person looks before sending. */}
      {image.file && (
        <div id="imagePreview" className="flex basis-full items-center gap-2 rounded-lg bg-surface px-2 py-1.5 text-xs text-ink-2">
          <img src={image.file.uri} alt="" className="size-8 rounded object-cover" />
          <span className="flex-1 truncate">{image.file.label}</span>
          <button type="button" className="cursor-pointer px-1" title="Remove attached image" aria-label="Remove attached image" onClick={image.removeFile}>
            ✕
          </button>
        </div>
      )}
      <input
        id="input"
        ref={input}
        className={cx(control, "min-w-0 flex-1 rounded-xl py-2.5 text-[0.95rem]")}
        autoComplete="off"
        placeholder="Ask something..."
        aria-label="Message"
        value={text}
        onChange={(event) => setText(event.target.value)}
      />
      <button
        type="submit"
        id="send"
        className="inline-flex cursor-pointer items-center gap-2 rounded-full bg-linear-135 from-accent to-accent-2 px-5 py-2.5 font-semibold text-accent-contrast shadow-card enabled:hover:brightness-110 disabled:cursor-default disabled:opacity-50"
        disabled={busy}
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="size-4">
          <line x1="22" y1="2" x2="11" y2="13" />
          <polygon points="22 2 15 22 11 13 2 9 22 2" />
        </svg>
        <span>Send</span>
      </button>
      {busy && (
        <button type="button" id="stop" className={cx(button, "rounded-xl px-4")} onClick={() => void stop()}>
          Stop
        </button>
      )}
      <div className="flex basis-full items-center gap-2">
        <input
          id="imageUrl"
          className={cx(control, "min-w-0 flex-1")}
          autoComplete="off"
          placeholder="Image URL (optional)"
          aria-label="Image URL"
          value={image.url}
          onChange={(event) => image.typeUrl(event.target.value)}
        />
        {/* A label for the file input that also accepts a drop: the keyboard path is the input itself. */}
        {/* oxlint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
        <label
          id="dropZone"
          htmlFor="imageFile"
          className={cx(
            "cursor-pointer whitespace-nowrap rounded-lg border border-dashed border-line-strong px-3 py-1.5 text-xs text-muted hover:bg-surface",
            dragging && "border-accent bg-accent-soft text-accent",
          )}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={drop}
        >
          📎 or drop/choose an image
        </label>
        <input
          id="imageFile"
          type="file"
          accept="image/*"
          hidden
          onChange={(event) => {
            image.pickFile(event.target.files?.[0]);
            // So picking the same file again after removing it still fires.
            event.target.value = "";
          }}
        />
      </div>
      <div className="basis-full text-[0.72rem] text-muted">
        Note: whether an attached image is seen depends on the model behind this deployment, and it has not been verified with this app
        (GRAPH_PATTERNS.md pattern 44).
      </div>
    </form>
  );
}
