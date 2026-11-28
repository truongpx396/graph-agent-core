import { useState } from "react";
import { button, cx, label } from "@/shared/lib/classes";
import { DOMAIN_INFO, SUGGESTED_QUESTIONS } from "./domains";
import { useWorkspace } from "./store";
import { tagsOf } from "./tags";

/** Clamped, so "Try asking" stays in sight in the half of the sidebar the brief shares with the domain picker. */
function DomainCard({ title, description }: { title: string; description: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div id="domainInfo" className="shrink-0 rounded-xl border border-l-4 border-line border-l-accent bg-elevated p-3.5 text-[0.82rem] leading-relaxed text-ink-2 shadow-card">
      <strong className="mb-1 block text-sm text-ink">{title}:</strong>
      <p className={cx(!open && "line-clamp-3")}>{description}</p>
      <button type="button" className="mt-1 cursor-pointer text-xs font-semibold text-accent" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "Show less" : "Show more"}
      </button>
    </div>
  );
}

export function DomainBrief({ onAsk }: { onAsk: (question: string) => void }) {
  const domain = useWorkspace((state) => state.domain);
  const info = DOMAIN_INFO[domain];
  const questions = SUGGESTED_QUESTIONS[domain] ?? [];
  return (
    <>
      {info && <DomainCard key={domain} title={info.title} description={info.description} />}
      {questions.length > 0 && (
        <div id="suggestedQuestionsSection">
          <h2 className={cx(label, "mb-2")} title="Tags say what a question is meant to exercise. A model tag applies when the router is on; a small model does not always follow the rest.">
            Try asking
          </h2>
          <div id="suggestedQuestions" className="flex flex-col gap-1.5">
            {questions.map((question) => (
              <button key={question.text} type="button" className={cx(button, "text-left")} onClick={() => onAsk(question.text)}>
                {question.text}
                <span className="mt-1 flex flex-wrap gap-1 empty:hidden">
                  {tagsOf(question).map((tag) => (
                    <span
                      key={tag.text}
                      className={cx(
                        "rounded border px-1 py-px text-[0.65rem] leading-tight [overflow-wrap:anywhere]",
                        tag.kind === "route" ? "border-accent font-semibold text-accent" : "border-line text-muted",
                      )}
                    >
                      {tag.text}
                    </span>
                  ))}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
    </>
  );
}
