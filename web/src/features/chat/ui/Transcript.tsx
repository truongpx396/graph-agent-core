import { useLayoutEffect, useRef } from "react";
import type { Entry } from "../model/entry";
import { useChat } from "../model/store";
import { AssistantMessage, loadEarlierClass } from "./AssistantMessage";

export interface History {
  entries: Entry[];
  /** The first page of an opened conversation is still on its way. */
  loading: boolean;
  hasEarlier: boolean;
  loadingEarlier: boolean;
  failed: boolean;
  loadEarlier: () => void;
}

export function Transcript({ history }: { history: History }) {
  const live = useChat((state) => state.entries);
  const send = useChat((state) => state.send);
  const decide = useChat((state) => state.decide);
  const element = useRef<HTMLDivElement>(null);
  const last = useRef<{ first: string | undefined; height: number }>({ first: undefined, height: 0 });

  useLayoutEffect(() => {
    const box = element.current;
    if (!box) return;
    const first = history.entries[0]?.id;
    // Earlier messages were put above: keep the one being read where it was. Anything else follows the newest line.
    if (last.current.first !== undefined && first !== undefined && first !== last.current.first) {
      box.scrollTop += box.scrollHeight - last.current.height;
    } else {
      box.scrollTop = box.scrollHeight;
    }
    last.current = { first, height: box.scrollHeight };
    // The effect reads the DOM those two produce: it has to rerun because they changed.
    // oxlint-disable-next-line react/exhaustive-effect-dependencies
  }, [history.entries, history.loading, live]);

  const ask = (text: string) => void send(text);
  return (
    <div id="messages" ref={element} className="min-h-0 flex-1 overflow-y-auto px-0.5 py-2">
      {history.hasEarlier && (
        <button type="button" className={loadEarlierClass} disabled={history.loadingEarlier} onClick={history.loadEarlier}>
          Load earlier messages
        </button>
      )}
      {history.failed && <div className="msg system my-2 text-center text-sm italic text-muted">Could not load that conversation.</div>}
      {/* Live lines wait for the history they follow: a restored approval must not show up above an empty conversation. */}
      {(history.loading ? [] : [...history.entries, ...live]).map((entry) => {
        if (entry.kind === "assistant") {
          return <AssistantMessage key={entry.id} entry={entry} onAsk={ask} onDecide={(approved) => void decide(approved)} />;
        }
        return (
          <div
            key={entry.id}
            className={
              entry.kind === "user"
                ? "msg user my-2 ml-auto w-fit max-w-[85%] whitespace-pre-wrap rounded-xl bg-linear-135 from-accent to-accent-2 px-3.5 py-2.5 text-accent-contrast shadow-card wrap-anywhere"
                : "msg system my-2 text-center text-sm italic text-muted wrap-anywhere"
            }
          >
            {entry.text}
          </div>
        );
      })}
    </div>
  );
}
