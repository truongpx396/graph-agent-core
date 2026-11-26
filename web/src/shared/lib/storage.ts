// Browser storage can throw (private windows, blocked site data). Losing a preference must never break the page.

export interface SafeStorage {
  get(key: string): string | null;
  set(key: string, value: string): void;
  remove(key: string): void;
}

function safe(area: () => Storage): SafeStorage {
  return {
    get(key) {
      try {
        return area().getItem(key);
      } catch {
        return null;
      }
    },
    set(key, value) {
      try {
        area().setItem(key, value);
      } catch {
        // Not remembered for the next load; this one still works.
      }
    },
    remove(key) {
      try {
        area().removeItem(key);
      } catch {
        // Nothing was stored.
      }
    },
  };
}

export const localStore = safe(() => window.localStorage);
export const sessionStore = safe(() => window.sessionStorage);
