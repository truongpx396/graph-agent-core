import { useState } from "react";
import { type Attachment, useChat } from "../model/store";

/** One image per turn: a picked or dropped file (as a data URI), or a URL. Choosing one clears the other. */
export function useAttachment() {
  const [file, setFile] = useState<Attachment | null>(null);
  const [url, setUrl] = useState("");

  function pickFile(picked: File | undefined): void {
    if (!picked) return;
    // `accept="image/*"` only steers the file picker: a DROPPED file arrives unfiltered, so this is the real check.
    if (!picked.type.startsWith("image/")) {
      useChat.getState().note("That doesn't look like an image file.");
      return;
    }
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      if (typeof reader.result !== "string") return;
      setFile({ uri: reader.result, label: picked.name });
      setUrl("");
    });
    reader.readAsDataURL(picked);
  }

  return {
    file,
    url,
    attachment: file ?? (url.trim() ? { uri: url.trim(), label: url.trim() } : null),
    pickFile,
    removeFile: () => setFile(null),
    typeUrl(next: string): void {
      setUrl(next);
      if (next.trim()) setFile(null);
    },
    clear(): void {
      setFile(null);
      setUrl("");
    },
  };
}
