// The module rules of this app, as a test rather than a convention (the same stance as tests/core/test_architecture_layers.py).
//   shared    knows nothing about features or the app
//   features  use shared and their own files; another feature only through its index, and only along the edges listed below
//   app       composes features, through their index files
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = resolve(import.meta.dirname, "..");
const FEATURE_EDGES: Record<string, string[]> = { sessions: ["chat", "workspace"] };
/** The files allowed to call `fetch`: the API client, and sign-in's code exchange with another origin. */
const MAY_FETCH = ["shared/api/http.ts", "features/auth/pkce.ts"];

function filesUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? filesUnder(path) : [path];
  });
}

const isSource = (path: string) => /\.tsx?$/.test(path) && !/\.test\.tsx?$/.test(path);
const sources = filesUnder(SRC)
  .filter(isSource)
  .map((path) => ({ path: relative(SRC, path).split(sep).join("/"), text: readFileSync(path, "utf8") }))
  .filter(({ path }) => !path.startsWith("test/"));

/** Where an import points, relative to src, or null for a package. */
function target(from: string, specifier: string): string | null {
  if (specifier.startsWith("@/")) return specifier.slice(2);
  if (!specifier.startsWith(".")) return null;
  return relative(SRC, resolve(SRC, from, "..", specifier)).split(sep).join("/");
}

const imports = (text: string) => [...text.matchAll(/(?:from|import)\s*\(?\s*"([^"]+)"/g)].map((match) => match[1] ?? "");

function violation(from: string, to: string): string | null {
  const [fromLayer, fromName] = from.split("/");
  const [toLayer, toName, ...rest] = to.split("/");
  if (toLayer === "app" && fromLayer !== "app" && from !== "main.tsx") return "only the app imports the app";
  if (fromLayer === "shared" && toLayer === "features") return "shared knows no feature";
  if (toLayer !== "features" || (fromLayer === "features" && fromName === toName)) return null;
  if (rest.length > 0) return "another feature is used through its index";
  if (fromLayer === "features" && !FEATURE_EDGES[fromName ?? ""]?.includes(toName ?? "")) return `${fromName} may not use ${toName}`;
  return null;
}

describe("module boundaries", () => {
  it("reads the sources it claims to check", () => {
    expect(sources.map((file) => file.path)).toContain("features/chat/model/store.ts");
  });

  it("every import follows the layering", () => {
    const broken = sources.flatMap(({ path, text }) =>
      imports(text).flatMap((specifier) => {
        const to = target(path, specifier);
        const reason = to === null ? null : violation(path, to);
        return reason ? [`${path} -> ${specifier}: ${reason}`] : [];
      }),
    );
    expect(broken).toEqual([]);
  });

  it("the rule itself rejects what it should", () => {
    expect(violation("shared/api/http.ts", "features/auth")).toBe("shared knows no feature");
    expect(violation("features/chat/model/store.ts", "features/auth")).toBe("chat may not use auth");
    expect(violation("features/sessions/queries.ts", "features/chat/model/store")).toBe("another feature is used through its index");
    expect(violation("features/sessions/queries.ts", "features/chat")).toBeNull();
    expect(violation("features/chat/api.ts", "app/wiring")).toBe("only the app imports the app");
  });

  it("no request skips the API client, which adds identity and handles a 401", () => {
    const callers = sources.filter(({ text }) => /(?<![\w.])fetch\(/.test(text.replace(/\/\/.*$/gm, ""))).map((file) => file.path);
    expect(callers.toSorted()).toEqual(MAY_FETCH.toSorted());
  });

  it("the page loads no script but its own bundle", () => {
    const page = readFileSync(join(SRC, "..", "index.html"), "utf8");
    expect([...page.matchAll(/<script[^>]*src="([^"]+)"/g)].map((match) => match[1])).toEqual(["/src/main.tsx"]);
    expect(page).toContain('<meta name="agent-auth" content="__AGENT_AUTH__" />');
  });
});
