import { button, cx, label } from "@/shared/lib/classes";
import { DOMAIN_INFO, SUGGESTED_QUESTIONS } from "./domains";
import { useWorkspace } from "./store";
import { tagsOf } from "./tags";

export function DomainBrief({ onAsk }: { onAsk: (question: string) => void }) {
  const domain = useWorkspace((state) => state.domain);
  const info = DOMAIN_INFO[domain];
  const questions = SUGGESTED_QUESTIONS[domain] ?? [];
  return (
    <>
      {info && (
        <div id="domainInfo" className="rounded-xl border border-l-4 border-line border-l-accent bg-elevated p-3.5 text-[0.82rem] leading-relaxed text-ink-2 shadow-card">
          <strong className="mb-1 block text-sm text-ink">{info.title}:</strong>
          {info.description}
        </div>
      )}
      {questions.length > 0 && (
        <div id="suggestedQuestionsSection">
          <h2 className={cx(label, "mb-2")}>Try asking</h2>
          <p className="mb-2 text-[0.7rem] leading-snug text-muted">
            Tags say what a question is meant to exercise. A model tag applies when the router is on; a small model does not always follow the rest.
          </p>
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
