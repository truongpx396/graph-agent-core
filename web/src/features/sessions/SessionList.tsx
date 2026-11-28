// The class name `session-item` and the ids `sessionList` and `newChat` are what the tests find things by.
import { useChat } from "@/features/chat";
import { button, cx, label } from "@/shared/lib/classes";
import { groupByAge } from "./group";
import { openSession } from "./openSession";
import { useSessions } from "./queries";

const item =
  "session-item block w-full cursor-pointer truncate rounded-lg px-2.5 py-1.5 text-left text-[0.82rem] text-ink-2 focus-visible:outline-2 focus-visible:outline-accent enabled:hover:bg-surface-hover disabled:cursor-default disabled:opacity-50 aria-[current=page]:bg-surface-hover aria-[current=page]:font-medium aria-[current=page]:text-ink aria-[current=page]:opacity-100";

export function SessionList({ ready, className }: { ready: boolean; className?: string }) {
  const threadId = useChat((state) => state.threadId);
  const streaming = useChat((state) => state.turn?.phase === "streaming");
  const startFresh = useChat((state) => state.startFresh);
  const { data: sessions = [], isLoading, isError } = useSessions(ready);
  // A brand-new thread is not listed until its first turn is recorded.
  const listed = sessions.some((session) => session.thread_id === threadId);

  return (
    <nav id="sessionList" aria-label="Conversations" className={cx("flex flex-col gap-2 px-2 pt-3", className)}>
      <div className="flex items-center justify-between gap-2 px-2">
        <h2 className={label}>Conversations</h2>
        <button type="button" id="newChat" className={cx(button, "text-xs")} disabled={streaming} onClick={startFresh}>
          + New chat
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto pb-3">
        {!listed && (
          <div className="rounded-lg bg-surface-hover px-2.5 py-1.5 text-[0.82rem] font-medium" aria-current="page">
            New conversation
          </div>
        )}
        {isLoading && <p className="px-2.5 py-1.5 text-xs text-muted">Loading…</p>}
        {isError && sessions.length === 0 && <p className="px-2.5 py-1.5 text-xs text-muted">Could not load your conversations.</p>}
        {groupByAge(sessions).map((group) => (
          <section key={group.label} aria-label={group.label}>
            <h3 className="px-2.5 pt-2.5 pb-1 text-[0.68rem] font-semibold text-muted">{group.label}</h3>
            <ul>
              {group.sessions.map((session) => {
                const current = session.thread_id === threadId;
                return (
                  <li key={session.thread_id}>
                    <button
                      type="button"
                      className={item}
                      title={session.title}
                      aria-current={current ? "page" : undefined}
                      disabled={streaming}
                      onClick={() => {
                        if (!current) void openSession(session.thread_id);
                      }}
                    >
                      {session.title}
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
      </div>
    </nav>
  );
}
