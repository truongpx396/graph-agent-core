// The properties of sign-in (specs/018) that must not be quietly lost. The real proof, a person signing in with a real browser against a
// real Casdoor, is `make authlimit-web-check`.
import { createHash } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import { fakeApi, json } from "@/test/fakeApi";
import { authorize, useAuth } from "./store";

const ISSUER = "https://auth.example.test";
const IDENTITY = { mode: "identity", issuer: ISSUER, audience: "https://api.example.test", scope: "api:chat", client_id_prefix: "api-" };
const TOKEN = "graph-agent-core:access-token";
const FLOW = "graph-agent-core:pkce-flow";

const jwt = (claims: object) => ["e30", Buffer.from(JSON.stringify(claims)).toString("base64url"), "sig"].join(".");
const later = () => Math.floor(Date.now() / 1000) + 600;

function pageIn(config: object | string, search = "") {
  document.head.innerHTML = `<meta name="agent-auth" content='${typeof config === "string" ? config : JSON.stringify(config)}'>`;
  const location = { origin: "https://api.example.test", pathname: "/", search, assign: vi.fn<(url: string) => void>() };
  const history = { replaceState: vi.fn<() => void>(() => void (location.search = "")) };
  vi.stubGlobal("location", location);
  vi.stubGlobal("history", history);
  return { location, history };
}

describe("header mode", () => {
  it.each([{ mode: "headers" }, "__AGENT_AUTH__"])("leaves requests as they are (%j)", async (config) => {
    pageIn(config);
    const api = fakeApi({});
    await useAuth.getState().init();
    expect(authorize({ "X-Tenant-Id": "t", "X-Domain": "ecorp" })).toEqual({ "X-Tenant-Id": "t", "X-Domain": "ecorp" });
    useAuth.getState().unauthorized();
    expect(useAuth.getState().note).toBeNull();
    expect(api.calls).toHaveLength(0);
  });
});

describe("identity mode", () => {
  it("drops the pickers' headers and invents no credential when signed out", async () => {
    pageIn(IDENTITY);
    await useAuth.getState().init();
    expect(authorize({ "X-Tenant-Id": "victim", "X-Principal-Id": "boss", "X-Domain": "ecorp" })).toEqual({ "X-Domain": "ecorp" });
  });

  it("sends a live token and forgets an expired one", async () => {
    pageIn(IDENTITY);
    const live = jwt({ name: "alice", owner: "ecorp", exp: later() });
    sessionStorage.setItem(TOKEN, live);
    await useAuth.getState().init();
    expect(useAuth.getState().who).toBe("alice · ecorp");
    expect(authorize({})).toEqual({ Authorization: `Bearer ${live}` });
    sessionStorage.setItem(TOKEN, jwt({ exp: later() - 1200 }));
    expect(authorize({})).toEqual({});
    expect(sessionStorage.getItem(TOKEN)).toBeNull();
  });

  it("starts with an S256 challenge, a state and the audience, and nothing secret in the URL", async () => {
    const { location } = pageIn(IDENTITY);
    await useAuth.getState().init();
    await useAuth.getState().signIn("  ecorp ");
    const url = new URL(location.assign.mock.calls[0]?.[0] ?? "");
    const flow = JSON.parse(sessionStorage.getItem(FLOW) ?? "{}") as { state: string; verifier: string };
    expect(url.origin + url.pathname).toBe(`${ISSUER}/login/oauth/authorize`);
    expect(Object.fromEntries(url.searchParams)).toEqual({
      client_id: "api-ecorp",
      response_type: "code",
      redirect_uri: "https://api.example.test/",
      scope: "api:chat",
      state: flow.state,
      code_challenge: createHash("sha256").update(flow.verifier).digest("base64url"),
      code_challenge_method: "S256",
      resource: "https://api.example.test",
    });
    expect(url.search).not.toContain(flow.verifier);
    expect(localStorage.getItem("graph-agent-core:organization")).toBe("ecorp");
  });

  it("refuses to start without an organization", async () => {
    const { location } = pageIn(IDENTITY);
    await useAuth.getState().init();
    await useAuth.getState().signIn("   ");
    expect(useAuth.getState().note).toBe("Enter your organization.");
    expect(location.assign).not.toHaveBeenCalled();
  });

  it("cleans the address BEFORE exchanging the code with the verifier, and keeps the token in this tab only", async () => {
    sessionStorage.setItem(FLOW, JSON.stringify({ state: "s1", verifier: "v1", org: "ecorp" }));
    const { history } = pageIn(IDENTITY, "?code=CODE123&state=s1");
    const token = jwt({ sub: "u1", exp: later() });
    const order: string[] = [];
    history.replaceState.mockImplementation(() => void order.push("clean"));
    const api = fakeApi({
      [`POST ${ISSUER}/api/login/oauth/access_token`]: () => {
        order.push("exchange");
        return json({ access_token: token });
      },
    });
    await useAuth.getState().init();
    expect(order).toEqual(["clean", "exchange"]);
    const body = Object.fromEntries(new URLSearchParams(api.calls[0]?.body as string));
    expect(body).toMatchObject({ grant_type: "authorization_code", code: "CODE123", code_verifier: "v1", resource: IDENTITY.audience });
    expect(sessionStorage.getItem(TOKEN)).toBe(token);
    expect(sessionStorage.getItem(FLOW)).toBeNull();
    expect(Object.keys(localStorage).map((key) => localStorage.getItem(key)).join()).not.toContain(token);
  });

  it.each([
    ["a state that is not ours", "?code=C&state=other", { state: "s1", verifier: "v1", org: "ecorp" }],
    ["no flow started here", "?code=C&state=s1", null],
    ["the server's own error", "?error=access_denied", { state: "s1", verifier: "v1", org: "ecorp" }],
  ])("makes no request for %s", async (_name, search, flow) => {
    if (flow) sessionStorage.setItem(FLOW, JSON.stringify(flow));
    pageIn(IDENTITY, search);
    const api = fakeApi({});
    await useAuth.getState().init();
    expect(api.calls).toHaveLength(0);
    expect(sessionStorage.getItem(TOKEN)).toBeNull();
    expect(useAuth.getState().note).toBeTruthy();
  });

  it("forgets the token on a 401 and asks again without redirecting", async () => {
    const { location } = pageIn(IDENTITY);
    sessionStorage.setItem(TOKEN, jwt({ sub: "u1", exp: later() }));
    await useAuth.getState().init();
    useAuth.getState().unauthorized();
    expect(sessionStorage.getItem(TOKEN)).toBeNull();
    expect(useAuth.getState()).toMatchObject({ who: null, note: "Your sign-in is no longer valid. Sign in again." });
    expect(location.assign).not.toHaveBeenCalled();
  });

  it("reloads on sign out, so the next person sees no conversation", async () => {
    const { location } = pageIn(IDENTITY);
    sessionStorage.setItem(TOKEN, jwt({ exp: later() }));
    await useAuth.getState().init();
    useAuth.getState().signOut();
    expect(sessionStorage.getItem(TOKEN)).toBeNull();
    expect(location.assign).toHaveBeenCalledWith("https://api.example.test/");
  });
});
