import { create } from "zustand";
import { localStore } from "@/shared/lib/storage";

export type Theme = "light" | "dark";
const KEY = "graph-agent-core:theme";
const systemDark = () => window.matchMedia?.("(prefers-color-scheme: dark)") ?? null;

function stored(): Theme | null {
  const value = localStore.get(KEY);
  return value === "light" || value === "dark" ? value : null;
}

export function effectiveTheme(): Theme {
  return stored() ?? (systemDark()?.matches ? "dark" : "light");
}

function apply(theme: Theme): Theme {
  document.documentElement.dataset["theme"] = theme;
  return theme;
}

export const useTheme = create<{ theme: Theme; toggle: () => void }>((set, get) => ({
  theme: "light",
  toggle() {
    const next: Theme = get().theme === "dark" ? "light" : "dark";
    localStore.set(KEY, next);
    set({ theme: apply(next) });
  },
}));

/** Call before the first render, so the page never flashes the wrong palette. Follows the system until the person picks a side. */
export function initTheme(): void {
  useTheme.setState({ theme: apply(effectiveTheme()) });
  systemDark()?.addEventListener("change", () => {
    if (stored() === null) useTheme.setState({ theme: apply(effectiveTheme()) });
  });
}
