/**
 * A conversation or list id, not a secret. `crypto.randomUUID` exists only in a secure context (HTTPS or localhost); reached by a bare
 * IP address it is missing, and calling it unguarded once left every button on the page dead.
 */
export function randomId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = Math.trunc(Math.random() * 16);
    return (c === "x" ? r : (r % 4) + 8).toString(16);
  });
}
