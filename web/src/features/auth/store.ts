import { create } from "zustand";
import { type AuthConfig, readAuthConfig } from "./config";
import { claimsOf, currentToken, finishSignIn, forgetSignIn, redirectUri, startSignIn } from "./pkce";

interface AuthState {
  config: AuthConfig;
  /** False until a return from the authorization server has been handled: the first request must carry the token it brings. */
  ready: boolean;
  /** "name · organization" of the signed-in person, or null. */
  who: string | null;
  note: string | null;
  init: () => Promise<void>;
  signIn: (organization: string) => Promise<void>;
  signOut: () => void;
  /** A 401 forgets the token and asks again. It never redirects by itself, so a misconfiguration cannot become a redirect loop. */
  unauthorized: () => void;
}

function signedInAs(): string | null {
  const token = currentToken();
  if (!token) return null;
  const { name, sub, owner } = claimsOf(token);
  return `${name || sub || "signed in"}${owner ? ` · ${owner}` : ""}`;
}

export const useAuth = create<AuthState>((set, get) => ({
  config: { mode: "headers" },
  ready: false,
  who: null,
  note: null,
  async init() {
    const config = readAuthConfig();
    set({ config });
    let note: string | null = null;
    if (config.mode === "identity") {
      note = await finishSignIn(config).catch(() => "The sign-in could not be completed. Try again.");
    }
    set({ ready: true, who: signedInAs(), note });
  },
  async signIn(organization) {
    const { config } = get();
    if (config.mode !== "identity") return;
    try {
      await startSignIn(config, organization);
    } catch (error) {
      set({ note: error instanceof Error ? error.message : String(error) });
    }
  },
  signOut() {
    forgetSignIn();
    // Reload rather than redraw: the conversation on screen was this person's, and the next one at this browser must not see it.
    window.location.assign(redirectUri());
  },
  unauthorized() {
    if (get().config.mode !== "identity") return;
    // A 401 on a first visit is only "not signed in yet"; after a login it means that login stopped working.
    const hadToken = currentToken() !== null;
    forgetSignIn();
    set({ who: null, note: hadToken ? "Your sign-in is no longer valid. Sign in again." : get().note });
  },
}));

/**
 * In identity mode the tenant and principal headers are dropped (the front door would remove them anyway) and the token is added.
 * Header mode leaves them as they are.
 */
export function authorize(headers: Record<string, string>): Record<string, string> {
  if (useAuth.getState().config.mode !== "identity") return headers;
  const { "X-Tenant-Id": _tenant, "X-Principal-Id": _principal, ...rest } = headers;
  const token = currentToken();
  return token ? { ...rest, Authorization: `Bearer ${token}` } : rest;
}
