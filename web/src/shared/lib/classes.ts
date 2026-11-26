export function cx(...classes: (string | false | null | undefined)[]): string {
  return classes.filter(Boolean).join(" ");
}

// The few control styles every feature shares, so they stay one look without a component library.
export const control =
  "rounded-lg border border-line bg-elevated px-2.5 py-1.5 text-sm text-ink transition-colors enabled:hover:border-line-strong focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50";
export const button = `${control} cursor-pointer disabled:cursor-default enabled:hover:bg-surface-hover`;
export const chip = `${button} rounded-full text-xs`;
export const label = "text-[0.7rem] font-bold uppercase tracking-wider text-muted";
