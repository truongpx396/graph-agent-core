import { useCallback, useLayoutEffect, useRef } from "react";
import { retryLine, skeleton } from "@/shared/lib/classes";
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
  retry: () => void;
}

/** How close to the end still counts as reading the newest line. */
const NEAR_END_PX = 80;

export function Transcript({ history }: { history: History }) {
  const live = useChat((state) => state.entries);
  const threadId = useChat((state) => state.threadId);
  const send = useChat((state) => state.send);
  const element = useRef<HTMLDivElement>(null);
  const last = useRef<{ first: string | undefined; height: number }>({ first: undefined, height: 0 });
  // Whether new text should pull the view down. Scrolling up turns it off, so an answer still streaming leaves the reader where they are.
  const follow = useRef(true);
  const top = useRef(0);
  // Changes with a message just sent or another conversation, which both go back to the newest line.
  const place = `${threadId} ${live.findLast((entry) => entry.kind === "user")?.id ?? ""}`;
  const seen = useRef(place);

  // Read on every scroll AND before every update: a token can arrive between the reader's scroll and its event, and would undo it.
  const look = useCallback((box: HTMLDivElement) => {
    const moved = box.scrollTop - top.current;
    const fromEnd = box.scrollHeight - box.scrollTop - box.clientHeight;
    // Only a move up lets go (content growing under a view at the end must not), and only coming back down takes hold again. A view
    // pushed up because the text under it shrank is still at the end.
    if (moved < 0 && fromEnd > 1) follow.current = false;
    else if (moved > 0 && fromEnd <= NEAR_END_PX) follow.current = true;
    top.current = box.scrollTop;
  }, []);

  useLayoutEffect(() => {
    const box = element.current;
    if (!box) return;
    look(box);
    if (seen.current !== place) {
      seen.current = place;
      follow.current = true;
    }
    const first = history.entries[0]?.id;
    // Earlier messages were put above: keep the one being read where it was. Anything else follows the newest line, unless the reader left it.
    if (last.current.first !== undefined && first !== undefined && first !== last.current.first) {
      box.scrollTop += box.scrollHeight - last.current.height;
    } else if (follow.current) {
      box.scrollTop = box.scrollHeight;
    }
    top.current = box.scrollTop;
    last.current = { first, height: box.scrollHeight };
    // The effect reads the DOM those two produce: it has to rerun because they changed.
    // oxlint-disable-next-line react/exhaustive-effect-dependencies
  }, [history.entries, history.loading, live, place, look]);

  const ask = (text: string) => void send(text);
  const empty = !history.loading && !history.failed && history.entries.length === 0 && live.length === 0;
  return (
    <div id="messages" ref={element} onScroll={(event) => look(event.currentTarget)} className="min-h-0 flex-1 overflow-y-auto px-0.5 py-2">
      {history.hasEarlier && (
        <button type="button" className={loadEarlierClass} disabled={history.loadingEarlier} onClick={history.loadEarlier}>
          Load earlier messages
        </button>
      )}
      {history.failed && (
        <div className={retryLine}>
          Couldn't load this conversation ·{" "}
          <button type="button" className="cursor-pointer font-semibold text-accent" onClick={history.retry}>
            Retry
          </button>
        </div>
      )}
      {history.loading && (
        <output aria-label="Loading the conversation" className="history-skeleton flex flex-col gap-3 py-2">
          <div className={`${skeleton} ml-auto h-9 w-2/5`} />
          <div className={`${skeleton} h-20 w-4/5`} />
        </output>
      )}
      {empty && <p className="empty-conversation mt-8 text-center text-sm text-muted">Ask a question to start, or pick one of the suggestions.</p>}
      {/* Live lines wait for the history they follow: a restored approval must not show up above an empty conversation. */}
      {(history.loading ? [] : [...history.entries, ...live]).map((entry) => {
        if (entry.kind === "assistant") return <AssistantMessage key={entry.id} entry={entry} onAsk={ask} />;
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
