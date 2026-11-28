// What is about to happen, in the server's words, and the two answers. The class names `approval` and `approval-question` are what the
// browser tests find it by (tests/live/).
//
// The shortcuts work only while the card itself has the focus, and the card never takes the focus: a person typing who presses Enter as a
// request appears must not have approved it.
import type { KeyboardEvent } from "react";
import { button, cx } from "@/shared/lib/classes";
import type { Approval } from "../model/entry";
import { Icon, TIER_NAMES } from "./icons";

interface ItemView {
  key: string;
  title: string;
  effect: string;
  tier: keyof typeof TIER_NAMES | null;
  target: string | null;
  visibleTo: string | null;
  reversible: boolean;
}

/** One row per call. A pause from an older server has no items: the cautious words, never the tool's name. */
function rows(approval: Approval): ItemView[] {
  return approval.calls.map((_call, index) => {
    const item = approval.items[index];
    return {
      key: item?.call_id ?? String(index),
      title: item?.title ?? "Run an action",
      effect: item?.effect ?? "Runs with the details below",
      tier: item?.tier ?? null,
      target: item?.target ?? null,
      visibleTo: item?.visible_to ?? null,
      reversible: item?.reversible ?? false,
    };
  });
}

function Item({ item }: { item: ItemView }) {
  return (
    <li className="approval-item py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">{item.title}</span>
        {item.tier && (
          <span className="inline-flex items-center gap-1 rounded-full border border-line px-2 py-0.5 text-[0.68rem] text-ink-2">
            <Icon name={item.tier} label="" className="size-3" />
            {TIER_NAMES[item.tier]}
          </span>
        )}
      </div>
      <p className="text-sm text-ink-2">{item.effect}</p>
      <p className="text-xs text-muted">
        {[item.target, item.visibleTo ? `Seen by: ${item.visibleTo}` : null, item.reversible ? "Can be undone" : "Can't be undone"].filter(Boolean).join(" · ")}
      </p>
    </li>
  );
}

export function ApprovalCard({ approval, onDecide }: { approval: Approval; onDecide: (approved: boolean) => void }) {
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onDecide(false);
    } else if (event.key === "Enter" && event.target === event.currentTarget && !event.repeat) {
      event.preventDefault();
      onDecide(true);
    }
  };
  const items = rows(approval);
  return (
    // The shortcuts are an extra: both answers are ordinary buttons.
    // oxlint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <section
      // oxlint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
      aria-labelledby="approvalQuestion"
      className="approval group mb-2 max-h-[45vh] shrink-0 overflow-y-auto rounded-xl border border-line-strong bg-elevated px-3.5 py-2.5 shadow-card focus-visible:outline-2 focus-visible:outline-accent"
      onKeyDown={onKeyDown}
    >
      <h2 id="approvalQuestion" className="approval-question text-xs font-semibold text-muted">
        {items.length === 1 ? "Approve this action?" : `Approve these ${items.length} actions?`}
      </h2>
      <ul className="divide-y divide-line">
        {items.map((item) => (
          <Item key={item.key} item={item} />
        ))}
      </ul>
      <details className="approval-details mb-2 text-xs text-muted">
        <summary className="cursor-pointer">Technical details</summary>
        {approval.calls.map((call, index) => (
          // Two calls can be alike in every way, and the list never changes.
          // oxlint-disable-next-line react/no-array-index-key
          <pre key={index} className="mt-1 overflow-x-auto rounded-lg bg-surface p-2">
            {call.name} {JSON.stringify(call.args, null, 2)}
          </pre>
        ))}
      </details>
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className="cursor-pointer rounded-lg bg-accent px-3.5 py-1.5 text-sm font-semibold text-accent-contrast hover:bg-accent-hover" onClick={() => onDecide(true)}>
          Approve
        </button>
        <button type="button" className={cx(button, "px-3.5")} onClick={() => onDecide(false)}>
          Decline
        </button>
        <span className="ml-auto hidden text-[0.7rem] text-muted group-focus:inline">⏎ approve · esc decline</span>
      </div>
    </section>
  );
}
