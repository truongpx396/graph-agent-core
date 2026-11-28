// A fenced block with its language and a copy button. Copying is a click and writes to the clipboard only: nothing is fetched or run.
import { type ReactNode, useEffect, useRef, useState } from "react";

export function CodeBlock({ language, children }: { language: string; children: ReactNode }) {
  const pre = useRef<HTMLPreElement>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const timer = copied ? setTimeout(() => setCopied(false), 2000) : undefined;
    return () => clearTimeout(timer);
  }, [copied]);

  // Absent on a page served over plain http from another host: no button then, rather than one that does nothing.
  const clipboard = "clipboard" in navigator ? navigator.clipboard : undefined;

  return (
    <div className="code-block">
      <div className="code-block-header">
        <span>{language || "text"}</span>
        {clipboard && (
          <button
            type="button"
            className="cursor-pointer font-semibold hover:text-ink"
            // Refused (no permission, an unfocused page): the button keeps saying "Copy".
            onClick={() => void clipboard.writeText(pre.current?.textContent ?? "").then(() => setCopied(true), () => setCopied(false))}
          >
            {copied ? "Copied" : "Copy"}
          </button>
        )}
      </div>
      <pre ref={pre}>{children}</pre>
    </div>
  );
}
