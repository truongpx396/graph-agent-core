// A turn's usage in the words the footer and the header show. Unknown is said to be unknown, never shown as zero.
import type { Entry, Usage } from "./entry";

export function tokensText(tokens: number | null): string {
  if (tokens === null) return "tokens unknown";
  return tokens < 1000 ? `${tokens} tokens` : `${(tokens / 1000).toFixed(1)}k tokens`;
}

export function costText(cost: number | null): string {
  if (cost === null) return "cost unknown";
  if (cost === 0) return "$0";
  if (cost < 0.0001) return "under $0.0001";
  return `$${cost < 0.01 ? cost.toPrecision(2) : cost.toFixed(2)}`;
}

/** The server's tier names, in words. `local` is the router's name for the default model, which need not be local. A name not listed is not shown. */
const TIERS = new Map([["local", "default tier"], ["medium", "medium tier"], ["strong", "strong tier"]]);

/** `model · tier · tokens · cost`. A turn no model answered (a cached answer, a refused message) says so. */
export function usageText(usage: Usage): string {
  if (usage.calls === 0) return "no model call";
  const model = usage.model?.split("/").at(-1) ?? "model unknown";
  return [model, TIERS.get(usage.route ?? ""), tokensText(usage.tokens), costText(usage.costUsd)].filter(Boolean).join(" · ");
}

const sum = (values: (number | null)[]) => values.reduce<number | null>((total, value) => (total === null || value === null ? null : total + value), 0);

/** The sum of the turns answered in this tab. One unknown part makes that total unknown. Null when no turn reported usage. */
export function totalOf(entries: Entry[]): { tokens: number | null; costUsd: number | null } | null {
  const used = entries.flatMap((entry) => (entry.kind === "assistant" && entry.usage ? [entry.usage] : []));
  if (used.length === 0) return null;
  return { tokens: sum(used.map((usage) => usage.tokens)), costUsd: sum(used.map((usage) => usage.costUsd)) };
}
