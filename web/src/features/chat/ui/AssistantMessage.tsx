// The class names `msg`, `status`, `tool-activity-line`, `draft-text`, `answer-text`, `approval`, `approval-question`, `citations` and
// `citation-item` are what the browser tests find things by (tests/live/): style with the utilities beside them, keep the names.
import { useState } from "react";
import type { Citation } from "@/shared/api";
import { button, chip, cx } from "@/shared/lib/classes";
import type { AssistantEntry } from "../model/entry";
import { Markdown } from "./Markdown";

const box = "my-2 rounded-lg border border-line bg-surface px-3 py-2";

function Choices({ options, onPick, className }: { options: string[]; onPick: (option: string) => void; className?: string }) {
  return (
    <div className={cx("flex flex-wrap gap-1.5", className)}>
      {options.map((option) => (
        <button key={option} type="button" className={chip} onClick={() => onPick(option)}>
          {option}
        </button>
      ))}
    </div>
  );
}

/** A source's title, linked to the original document when there is one. The server only sends http(s); checked again because an `href` runs. */
function SourceName({ citation }: { citation: Citation }) {
  const url = citation.source_url;
  if (!url || !/^https?:\/\//i.test(url)) return <span>{citation.title}</span>;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer" title={url} className="underline underline-offset-2">
      {citation.title}
    </a>
  );
}

interface Props {
  entry: AssistantEntry;
  onAsk: (text: string) => void;
  onDecide: (approved: boolean) => void;
}

export function AssistantMessage({ entry, onAsk, onDecide }: Props) {
  const { status, tools, draft, answer, notice, clarification, approval, citations, followups, muted } = entry;
  // One element for what is committed and what is still streaming, so committing a draft changes nothing on screen.
  const text = [answer, draft].filter(Boolean).join("\n\n");
  // True from the first streamed text on; a message replayed from history never streams, so never fades.
  const [streamed, setStreamed] = useState(false);
  if (draft && !streamed) setStreamed(true);
  return (
    <div className="msg assistant my-2 max-w-[85%] rounded-xl border border-line bg-elevated px-3.5 py-2.5 shadow-card wrap-anywhere">
      {status && <div className="status mb-1 text-sm text-muted">{status}</div>}
      <div className="tool-activity text-xs text-muted">
        {tools.map((item, index) => (
          // Append-only list: the index is a stable identity.
          // oxlint-disable-next-line react/no-array-index-key
          <div key={index} className={cx("tool-activity-line my-0.5", item.done && "done opacity-75")}>
            {item.done ? "✓" : "🔧"} {item.tool}({JSON.stringify(item.args)})
          </div>
        ))}
      </div>
      {clarification && (
        <div className={cx("clarification", box)}>
          <div className="mb-1.5 text-sm">{clarification.question}</div>
          <Choices options={clarification.options} onPick={onAsk} />
        </div>
      )}
      {approval && (
        <div className={cx("approval", box)}>
          <div className="approval-question mb-1.5 text-sm">
            ⏸ Approve this action? {approval.map((call) => `${call.name}(${JSON.stringify(call.args)})`).join(", ")}
          </div>
          <div className="flex flex-wrap gap-1.5">
            <button type="button" className={chip} onClick={() => onDecide(true)}>
              Approve
            </button>
            <button type="button" className={cx(chip, "border-danger! bg-danger-soft! text-danger!")} onClick={() => onDecide(false)}>
              Reject
            </button>
          </div>
        </div>
      )}
      {text && (
        <div className={cx("answer-text markdown", draft && "draft-text", streamed && "streamed", muted && "mt-2 text-xs opacity-65")}>
          <Markdown text={text} streaming={Boolean(draft)} animate={streamed} />
        </div>
      )}
      {notice && <span className="answer-text block">{notice}</span>}
      {citations.length > 0 && (
        <div className="citations mt-2.5 flex flex-col gap-0.5 border-t border-line pt-2 text-xs text-muted">
          <div className="text-[0.7rem] font-bold uppercase tracking-wide">Sources</div>
          {citations.map((citation) => (
            <div key={`${citation.marker}${citation.doc_id}`} className="citation-item flex gap-1.5">
              <span className="shrink-0 opacity-70">{citation.marker}</span>
              <SourceName citation={citation} />
            </div>
          ))}
        </div>
      )}
      {followups.length > 0 && <Choices className="followups mt-2.5" options={followups} onPick={onAsk} />}
    </div>
  );
}

export const loadEarlierClass = cx(button, "load-earlier mx-auto my-1.5 block rounded-full");
