import type { SessionSummary } from "@/shared/api";

export interface SessionGroup {
  label: string;
  sessions: SessionSummary[];
}

/** How many local calendar days back each heading reaches. Anything older, or with a date this build cannot read, is "Older". */
const AGES: [label: string, days: number][] = [
  ["Today", 0],
  ["Yesterday", 1],
  ["Previous 7 days", 7],
  ["Previous 30 days", 30],
];

/** Groups by last activity, keeping the server's order (newest first) inside each group. Empty groups are left out. */
export function groupByAge(sessions: SessionSummary[], now = new Date()): SessionGroup[] {
  // Built from the date's parts, not by subtracting 24 hours: a day is 23 or 25 hours when the clocks change.
  const startsAt = (days: number) => new Date(now.getFullYear(), now.getMonth(), now.getDate() - days).getTime();
  const groups: SessionGroup[] = [...AGES.map(([label]) => ({ label, sessions: [] })), { label: "Older", sessions: [] }];
  for (const session of sessions) {
    const at = Date.parse(session.last_active_at);
    const found = AGES.findIndex(([, days]) => at >= startsAt(days));
    groups[found === -1 ? AGES.length : found]?.sessions.push(session);
  }
  return groups.filter((group) => group.sessions.length > 0);
}
