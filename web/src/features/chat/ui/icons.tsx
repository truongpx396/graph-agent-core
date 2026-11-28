// Small line icons. A step's tier is shown by shape and named for a screen reader; colour carries nothing on its own.
import type { Tier } from "@/shared/api";

const PATHS = {
  read_only: "M11 4a7 7 0 1 0 0 14a7 7 0 0 0 0-14ZM20 20l-4-4",
  mutating: "M4 20h4L19 9l-4-4L4 16v4ZM13 7l4 4",
  outward: "M14 4h6v6M20 4l-8 8M18 14v6H4V6h6",
  unknown: "M12 10a2 2 0 1 0 0 4a2 2 0 0 0 0-4Z",
  lock: "M6 11h12v9H6v-9ZM8 11V8a4 4 0 0 1 8 0v3",
} as const;

export const TIER_NAMES: Record<Tier, string> = { read_only: "Reads", mutating: "Changes data", outward: "Reaches outside" };

/** `label` names the shape for a screen reader; leave it empty where the words beside the icon already say it. */
export function Icon({ name, label, className = "size-3.5" }: { name: keyof typeof PATHS; label: string; className?: string }) {
  return (
    <>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" data-icon={name} className={`${className} shrink-0`}>
        <path d={PATHS[name]} />
      </svg>
      {label && <span className="sr-only">{label}: </span>}
    </>
  );
}
