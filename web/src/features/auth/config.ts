// What the server tells the page about sign-in, in a <meta> tag it renders (app/adapters/inbound/api/web_auth.py). The tag carries no
// secret. Missing, unreplaced or unreadable means header mode: the API run alone.

export type AuthConfig =
  | { mode: "headers" }
  | { mode: "identity"; issuer: string; audience: string; scope: string; client_id_prefix: string };

export function readAuthConfig(): AuthConfig {
  const tag = document.querySelector<HTMLMetaElement>('meta[name="agent-auth"]');
  try {
    const config = JSON.parse(tag?.content ?? "") as AuthConfig;
    return config.mode === "identity" ? config : { mode: "headers" };
  } catch {
    return { mode: "headers" };
  }
}
