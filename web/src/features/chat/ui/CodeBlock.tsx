// A fenced block with its language and a copy button. Copying is a click and writes to the clipboard only: nothing is fetched or run.
// A block of Markdown source also has a preview, shown first.
import { type ReactNode, useEffect, useState } from "react";

interface Props {
  language: string;
  /** The block's own text: what Copy puts on the clipboard, in either view. */
  code: string;
  /** The source as highlighted elements. */
  children: ReactNode;
  preview?: ReactNode;
}

const action = "cursor-pointer font-semibold hover:text-ink";

export function CodeBlock({ language, code, children, preview }: Props) {
  const [copied, setCopied] = useState(false);
  const [source, setSource] = useState(false);

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
        <span className="flex gap-3">
          {preview !== undefined && (
            <button type="button" className={action} onClick={() => setSource(!source)}>
              {source ? "Preview" : "Source"}
            </button>
          )}
          {clipboard && (
            <button
              type="button"
              className={action}
              // Refused (no permission, an unfocused page): the button keeps saying "Copy".
              onClick={() => void clipboard.writeText(code).then(() => setCopied(true), () => setCopied(false))}
            >
              {copied ? "Copied" : "Copy"}
            </button>
          )}
        </span>
      </div>
      {preview !== undefined && !source ? <div className="markdown code-block-preview">{preview}</div> : <pre>{children}</pre>}
    </div>
  );
}
