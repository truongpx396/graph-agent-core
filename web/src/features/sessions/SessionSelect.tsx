import { useChat } from "@/features/chat";
import { control, label } from "@/shared/lib/classes";
import { openSession } from "./openSession";
import { useSessions } from "./queries";

export function SessionSelect({ ready }: { ready: boolean }) {
  const threadId = useChat((state) => state.threadId);
  const streaming = useChat((state) => state.turn?.phase === "streaming");
  const startFresh = useChat((state) => state.startFresh);
  const { data: sessions = [] } = useSessions(ready);
  const pick = (picked: string) => {
    if (!picked) startFresh();
    else if (picked !== threadId) void openSession(picked);
  };
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor="sessionSelect" className={label}>
        Conversation
      </label>
      <select
        id="sessionSelect"
        className={control}
        title="Switch conversation"
        disabled={streaming}
        // A brand-new thread is not listed until its first turn is recorded.
        value={sessions.some((session) => session.thread_id === threadId) ? threadId : ""}
        onChange={(event) => pick(event.target.value)}
      >
        <option value="">+ New conversation</option>
        {sessions.map((session) => (
          <option key={session.thread_id} value={session.thread_id}>
            {session.title}
          </option>
        ))}
      </select>
    </div>
  );
}
