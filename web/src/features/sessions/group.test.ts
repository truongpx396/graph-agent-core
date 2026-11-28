import { describe, expect, it } from "vitest";
import { groupByAge } from "./group";

// Local time on both sides, so the test means the same in every timezone.
const now = new Date(2026, 5, 15, 9, 0);
const at = (title: string, ...parts: [number, number, number, number?]) => {
  const stamp = new Date(parts[0], parts[1], parts[2], parts[3] ?? 12).toISOString();
  return { thread_id: title, title, created_at: stamp, last_active_at: stamp };
};
const titles = (sessions: Parameters<typeof groupByAge>[0]) =>
  groupByAge(sessions, now).map((group) => [group.label, group.sessions.map((session) => session.title)]);

describe("the conversation list's headings", () => {
  it("go by calendar day, not by hours elapsed", () => {
    expect(
      titles([
        at("this morning", 2026, 5, 15, 0),
        at("late last night", 2026, 5, 14, 23),
        at("a week ago", 2026, 5, 8, 0),
        at("last month", 2026, 4, 20),
        at("long ago", 2026, 0, 1),
      ]),
    ).toEqual([
      ["Today", ["this morning"]],
      ["Yesterday", ["late last night"]],
      ["Previous 7 days", ["a week ago"]],
      ["Previous 30 days", ["last month"]],
      ["Older", ["long ago"]],
    ]);
  });

  it("keep the server's order and skip empty headings", () => {
    expect(titles([at("newer", 2026, 5, 15, 8), at("older", 2026, 5, 15, 1)])).toEqual([["Today", ["newer", "older"]]]);
  });

  it("never lose a conversation whose date cannot be read", () => {
    expect(titles([{ thread_id: "x", title: "odd", created_at: "", last_active_at: "" }])).toEqual([["Older", ["odd"]]]);
  });
});
