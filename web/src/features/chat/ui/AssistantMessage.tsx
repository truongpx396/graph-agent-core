// The class names `msg`, `status`, `tool-activity-line`, `draft-text`, `answer-text`, `citations` and `citation-item` are what the
// browser tests find things by (tests/live/): style with the utilities beside them, keep the names.
import { useState } from "react";
import type { Citation } from "@/shared/api";
import { button, chip, cx } from "@/shared/lib/classes";
import type { AssistantEntry, Notice } from "../model/entry";
import { usageText } from "../model/usage";
import { ActivityRail } from "./ActivityRail";
import { Icon } from "./icons";
import { Markdown } from "./Markdown";
import { StatusLine } from "./StatusLine";

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

/** Only a failure is red. A refusal is neutral with a lock: nothing broke, a rule said no. */
function NoticeLine({ notice }: { notice: Notice }) {
  return (
    <div className={cx("notice mt-1 flex items-start gap-1.5 text-sm", notice.tone === "failed" ? "text-danger" : "text-ink-2")} data-tone={notice.tone}>
      {notice.tone === "blocked" && <Icon name="lock" label="Blocked" className="mt-0.5 size-3.5" />}
      <span>
        {notice.text}
        {notice.hint && <span className="text-muted"> {notice.hint}</span>}
      </span>
    </div>
  );
}

export function AssistantMessage({ entry, onAsk }: { entry: AssistantEntry; onAsk: (text: string) => void }) {
  const { status, started, since, steps, done, workedMs, draft, answer, notice, clarification, approval, declined, usage, citations, followups, muted } = entry;
  // One element for what is committed and what is still streaming, so committing a draft changes nothing on screen.
  const text = [answer, draft].filter(Boolean).join("\n\n");
  // True from the first streamed text on; a message replayed from history never streams, so never fades.
  const [streamed, setStreamed] = useState(false);
  if (draft && !streamed) setStreamed(true);
  return (
    <div className="msg assistant my-2 max-w-[85%] rounded-xl border border-line bg-elevated px-3.5 py-2.5 shadow-card wrap-anywhere">
      <ActivityRail steps={steps} done={done} workedMs={workedMs} />
      {status && <StatusLine status={status} started={started} since={since} />}
      {clarification && (
        <div className={cx("clarification", box)}>
          <div className="mb-1.5 text-sm">{clarification.question}</div>
          <Choices options={clarification.options} onPick={onAsk} />
        </div>
      )}
      {declined.length > 0 && <div className="declined mb-1 text-sm text-muted">Declined: {declined.join(", ")}. Nothing was changed by it.</div>}
      {text && (
        <div className={cx("answer-text markdown", draft && "draft-text", streamed && "streamed", muted && "mt-2 text-xs opacity-65")}>
          <Markdown text={text} streaming={Boolean(draft)} animate={streamed} />
        </div>
      )}
      {/* The card itself is above the composer; this says why the answer stopped here. */}
      {approval && <div className="mt-1 text-sm text-muted">Waiting for your approval</div>}
      {notice && <NoticeLine notice={notice} />}
      {citations.length > 0 && (
        <div className="citations mt-2.5 flex flex-col gap-0.5 border-t border-line pt-2 text-xs text-muted">
          <div className="font-semibold">Sources</div>
          {citations.map((citation) => (
            <div key={`${citation.marker}${citation.doc_id}`} className="citation-item flex gap-1.5">
              <span className="shrink-0 opacity-70">{citation.marker}</span>
              <SourceName citation={citation} />
            </div>
          ))}
        </div>
      )}
      {followups.length > 0 && <Choices className="followups mt-2.5" options={followups} onPick={onAsk} />}
      {usage && (
        <div className="usage mt-2 font-mono text-[0.68rem] text-muted" title={usage.model ?? undefined}>
          {usageText(usage)}
        </div>
      )}
    </div>
  );
}

export const loadEarlierClass = cx(button, "load-earlier mx-auto my-1.5 block rounded-full");
