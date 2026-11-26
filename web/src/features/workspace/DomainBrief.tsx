import { button, cx, label } from "@/shared/lib/classes";
import { DOMAIN_INFO, SUGGESTED_QUESTIONS } from "./domains";
import { useWorkspace } from "./store";

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
          <div id="suggestedQuestions" className="flex flex-col gap-1.5">
            {questions.map((question) => (
              <button key={question} type="button" className={cx(button, "text-left")} onClick={() => onAsk(question)}>
                {question}
              </button>
            ))}
          </div>
        </div>
      )}
    </>
  );
}
