// Sign-in (specs/018): authorization code with PKCE, as a PUBLIC client. Properties that must hold, each tested in auth.test.ts:
//   * no library: the Web Crypto API and fetch only;
//   * the token lives in sessionStorage (this tab), never in localStorage, a cookie or a URL;
//   * the code is exchanged with an S256 verifier and a `state` that must come back unchanged, and leaves the address bar before that.
import { localStore, sessionStore } from "@/shared/lib/storage";
import type { AuthConfig } from "./config";

type Identity = Extract<AuthConfig, { mode: "identity" }>;

const TOKEN_KEY = "graph-agent-core:access-token";
const FLOW_KEY = "graph-agent-core:pkce-flow";
const ORG_KEY = "graph-agent-core:organization";

export interface Claims {
  name?: string;
  sub?: string;
  owner?: string;
  exp?: number;
}

function base64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/, "");
}

const randomSecret = (bytes: number) => base64url(crypto.getRandomValues(new Uint8Array(bytes)));

async function challengeOf(verifier: string): Promise<string> {
  return base64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
}

/** For DISPLAY only ("signed in as ..."). Nothing is decided from these: the front door verifies the signature. */
export function claimsOf(token: string): Claims {
  try {
    const body = (token.split(".")[1] ?? "").replaceAll("-", "+").replaceAll("_", "/");
    const bytes = Uint8Array.from(atob(body + "=".repeat((4 - (body.length % 4)) % 4)), (c) => c.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes)) as Claims;
  } catch {
    return {};
  }
}

export function currentToken(): string | null {
  const token = sessionStore.get(TOKEN_KEY);
  if (!token) return null;
  const { exp } = claimsOf(token);
  if (typeof exp === "number" && exp * 1000 <= Date.now()) {
    sessionStore.remove(TOKEN_KEY);
    return null;
  }
  return token;
}

export const lastOrganization = () => localStore.get(ORG_KEY) ?? "ecorp";

export function forgetSignIn(): void {
  sessionStore.remove(TOKEN_KEY);
  sessionStore.remove(FLOW_KEY);
}

/** The page's own address is the redirect target: the authorization server compares it exactly with what was registered. */
export const redirectUri = () => `${window.location.origin}/`;

export async function startSignIn(config: Identity, organization: string): Promise<void> {
  const org = organization.trim();
  if (!org) throw new Error("Enter your organization.");
  const verifier = randomSecret(48);
  const state = randomSecret(16);
  sessionStore.set(FLOW_KEY, JSON.stringify({ state, verifier, org }));
  localStore.set(ORG_KEY, org);
  const query = new URLSearchParams({
    client_id: config.client_id_prefix + org,
    response_type: "code",
    redirect_uri: redirectUri(),
    scope: config.scope,
    state,
    code_challenge: await challengeOf(verifier),
    code_challenge_method: "S256",
    // RFC 8707: the token's audience. Without it the server mints one for the client id, which the front door refuses.
    resource: config.audience,
  });
  window.location.assign(`${config.issuer}/login/oauth/authorize?${query.toString()}`);
}

function readFlow(): { state: string; verifier: string; org: string } | null {
  try {
    return JSON.parse(sessionStore.get(FLOW_KEY) ?? "null") as ReturnType<typeof readFlow>;
  } catch {
    return null;
  }
}

/** If this load is the return from the authorization server, exchange the code. Resolves to a message to show, or null. */
export async function finishSignIn(config: Identity): Promise<string | null> {
  const params = new URLSearchParams(window.location.search);
  const code = params.get("code");
  const error = params.get("error");
  if (!code && !error) return null;
  // Single-use, but it has no business staying in the address bar or history.
  window.history.replaceState(null, "", window.location.pathname);
  const flow = readFlow();
  sessionStore.remove(FLOW_KEY);
  if (error) return `The authorization server refused the sign-in (${error}).`;
  if (!code || !flow || params.get("state") !== flow.state) return "That sign-in did not start in this tab, so it was ignored. Sign in again.";
  const response = await fetch(`${config.issuer}/api/login/oauth/access_token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: config.client_id_prefix + flow.org,
      code,
      redirect_uri: redirectUri(),
      code_verifier: flow.verifier,
      // Again: the audience is minted from the request that EXCHANGES the code (measured on Casdoor 4.24).
      resource: config.audience,
    }).toString(),
  });
  const body = (await response.json().catch(() => ({}))) as { access_token?: string };
  if (!response.ok || !body.access_token) return "The sign-in could not be completed. Try again.";
  sessionStore.set(TOKEN_KEY, body.access_token);
  return null;
}
