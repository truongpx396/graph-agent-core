// The one place this app calls its own API (src/test/architecture.test.ts holds that), so identity headers and the 401 rule cannot be
// skipped by a new caller.

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Who and where the next request is for. Bound once by the composition root (src/app/wiring.ts). */
export interface RequestContext {
  headers: () => Record<string, string>;
  /** The API refused the caller's credential. */
  onUnauthorized: () => void;
}

let context: RequestContext = { headers: () => ({}), onUnauthorized: () => {} };

export function bindRequestContext(next: RequestContext): void {
  context = next;
}

export interface ApiInit {
  method?: "GET" | "POST";
  json?: unknown;
  /** multipart/form-data: the browser sets Content-Type itself, because only it knows the boundary. */
  form?: FormData;
  signal?: AbortSignal;
}

export async function apiFetch(path: `/${string}`, init: ApiInit = {}): Promise<Response> {
  // A protocol-relative `//host/...` would carry the access token to another origin.
  if (path.startsWith("//")) throw new Error(`not a same-origin path: ${path}`);
  const headers = context.headers();
  if (init.json !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: init.method ?? "GET",
    headers,
    body: init.form ?? (init.json === undefined ? null : JSON.stringify(init.json)),
    signal: init.signal ?? null,
  });
  if (response.status === 401) context.onUnauthorized();
  return response;
}

/** The server's own words for a refused request (the error envelope or FastAPI's `detail`), else the status. */
export async function errorFrom(response: Response): Promise<ApiError> {
  const body: unknown = await response.json().catch(() => null);
  const record = typeof body === "object" && body !== null ? (body as Record<string, unknown>) : {};
  const text = [record["message"], record["detail"]].find((value) => typeof value === "string" && value !== "");
  return new ApiError(response.status, typeof text === "string" ? text : `HTTP ${response.status}`);
}

export async function apiJson<T>(path: `/${string}`, init?: ApiInit): Promise<T> {
  const response = await apiFetch(path, init);
  if (!response.ok) throw await errorFrom(response);
  return (await response.json()) as T;
}
