// What the agent did, one row per tool call, in the server's words. A tool's identifier is never shown. While the turn runs the rows are
// open and only the running one moves; afterwards they fold to one line.
import { useState } from "react";
import { cx } from "@/shared/lib/classes";
import type { Step } from "../model/entry";
import { Icon, TIER_NAMES } from "./icons";

function seconds(ms: number): string {
  if (ms < 50) return "<0.1 s";
  return ms < 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms / 1000)} s`;
}

function resultOf(step: Step): string | null {
  if (step.outcome === "refused") return "Not allowed";
  // Neutral: a step the agent recovers from is not the turn failing.
  if (step.outcome === "error") return "Didn't work";
  return step.summary;
}

function Row({ step }: { step: Step }) {
  const result = resultOf(step);
  return (
    <li className={cx("tool-activity-line flex items-center gap-1.5 py-0.5", step.outcome !== "running" && "done")}>
      {step.outcome === "refused" ? <Icon name="lock" label="Blocked" /> : <Icon name={step.tier ?? "unknown"} label={step.tier ? TIER_NAMES[step.tier] : "Step"} />}
      <span className={cx("min-w-0 flex-1 truncate", step.outcome === "running" && "shimmer")}>{step.label}</span>
      {result && <span className="max-w-[50%] truncate text-ink-2">{result}</span>}
      {step.durationMs !== null && <span className="shrink-0 font-mono text-[0.7rem]">{seconds(step.durationMs)}</span>}
    </li>
  );
}

/** Consecutive calls by one specialist sit under its name. */
function groups(steps: Step[]): { subagent: string | null; steps: Step[] }[] {
  return steps.reduce<{ subagent: string | null; steps: Step[] }[]>((all, step) => {
    const last = all.at(-1);
    if (last && last.subagent === step.subagent) last.steps.push(step);
    else all.push({ subagent: step.subagent, steps: [step] });
    return all;
  }, []);
}

export function ActivityRail({ steps, done, workedMs }: { steps: Step[]; done: boolean; workedMs: number }) {
  const [opened, setOpened] = useState(false);
  if (steps.length === 0) return null;
  const open = !done || opened;
  const count = `${steps.length} step${steps.length === 1 ? "" : "s"}`;
  return (
    <div className="tool-activity mb-1.5 text-xs text-muted">
      {done && (
        <button type="button" className="activity-summary cursor-pointer font-medium hover:text-ink" aria-expanded={opened} onClick={() => setOpened(!opened)}>
          Worked for {seconds(workedMs)} · {count} {opened ? "▾" : "▸"}
        </button>
      )}
      {open &&
        groups(steps).map((group) => (
          <div key={group.steps[0]?.callId} className={cx(group.subagent && "ml-2 border-l border-line pl-2.5")}>
            {group.subagent && <div className="py-0.5">Asked the {group.subagent} subagent</div>}
            <ul>
              {group.steps.map((step) => (
                <Row key={step.callId} step={step} />
              ))}
            </ul>
          </div>
        ))}
    </div>
  );
}
