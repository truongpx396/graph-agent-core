import { useMemo } from "react";
import { useChat } from "../model/store";
import { costText, tokensText, totalOf } from "../model/usage";

/** The header's sum of the turns answered in this tab since the conversation was opened. */
export function UsageTotal() {
  const entries = useChat((state) => state.entries);
  const total = useMemo(() => totalOf(entries), [entries]);
  if (!total) return null;
  return (
    <span id="usageTotal" className="font-mono text-[0.7rem] text-muted" title="Used by the turns answered here since this conversation was opened">
      {tokensText(total.tokens)} · {costText(total.costUsd)}
    </span>
  );
}
