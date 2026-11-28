// What is happening now. Until a worker has the job, a long wait is said plainly and stops animating: "thinking" and "nobody has this
// yet" must not look alike.
import { useEffect, useState } from "react";
import { cx } from "@/shared/lib/classes";

/** Below the server's own 30 s wait for a worker (`CHAT_FIRST_RESPONSE_DEADLINE_SECONDS`), or this would never be seen. */
export const WAITING_AFTER_MS = 10_000;

function useWaiting(since: number | null): boolean {
  const [waited, setWaited] = useState<number | null>(null);
  useEffect(() => {
    if (since === null) return undefined;
    const timer = setTimeout(() => setWaited(since), Math.max(since + WAITING_AFTER_MS - Date.now(), 0));
    return () => clearTimeout(timer);
  }, [since]);
  // Held against the wait it was measured for, so a new wait starts unexpired.
  return since !== null && waited === since;
}

export function StatusLine({ status, started, since }: { status: string; started: boolean; since: number | null }) {
  const waiting = useWaiting(started ? null : since);
  return (
    <output className={cx("status mb-1 block text-sm text-muted", !waiting && "shimmer")}>{waiting ? "Waiting to start" : status}</output>
  );
}
