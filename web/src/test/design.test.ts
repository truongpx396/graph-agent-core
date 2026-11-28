// The rules of web/DESIGN.md a machine can hold.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = resolve(import.meta.dirname, "..");
const filesUnder = (dir: string): string[] =>
  readdirSync(dir).flatMap((name) => (statSync(join(dir, name)).isDirectory() ? filesUnder(join(dir, name)) : [join(dir, name)]));
const sources = filesUnder(SRC)
  .filter((path) => /\.(tsx?|css)$/.test(path) && !/\.test\.tsx?$/.test(path) && !path.endsWith("schema.gen.ts"))
  .map((path) => ({ path: relative(SRC, path).split(sep).join("/"), text: readFileSync(path, "utf8") }));

/** A literal colour: #abc, #aabbcc, rgb(), hsl(). */
const RAW_COLOUR = /#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})\b|\b(?:rgba?|hsla?)\(/gi;
/** Tailwind's own palette (`text-red-500`), which would bypass the tokens as surely as a hex value. */
const PALETTE = /\b(?:bg|text|border|ring|from|to|via|fill|stroke|accent|outline)-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose|white|black)(?:-\d{2,3})?\b/g;
/** The blocks of app/styles.css that define the tokens. */
const TOKEN_BLOCKS = /(?::root(?:\[[^\]]*\]|:not\([^)]*\))?|@theme inline)\s*\{[^{}]*\}/g;

const outsideTheTokens = (path: string, text: string) => (path === "app/styles.css" ? text.replace(TOKEN_BLOCKS, "") : text);

describe("web/DESIGN.md", () => {
  it("reads the sources it claims to check", () => {
    expect(sources.map((file) => file.path)).toEqual(expect.arrayContaining(["app/styles.css", "features/chat/ui/ApprovalCard.tsx"]));
  });

  it("takes every colour from the tokens in app/styles.css", () => {
    const raw = sources.flatMap(({ path, text }) =>
      [...(outsideTheTokens(path, text).match(RAW_COLOUR) ?? []), ...(text.match(PALETTE) ?? [])].map((found) => `${path}: ${found}`),
    );
    expect(raw).toEqual([]);
  });

  it("the rule itself rejects what it should", () => {
    expect("color: #fff; background: rgb(0 0 0)".match(RAW_COLOUR)).toHaveLength(2);
    expect('className="text-red-500 bg-white"'.match(PALETTE)).toHaveLength(2);
    expect('className="text-danger bg-surface"'.match(PALETTE)).toBeNull();
    expect(outsideTheTokens("app/styles.css", ":root { --a: #fff; }\n.x { color: #000; }").match(RAW_COLOUR)).toEqual(["#000"]);
  });

  it("puts no tool's identifier on screen: the server's label or nothing", () => {
    // `call.name` and `step.tool` may be used to pair and to detect, and shown only inside the approval card's Technical details.
    const shown = sources
      .filter(({ path }) => path.endsWith(".tsx") && path !== "features/chat/ui/ApprovalCard.tsx")
      .flatMap(({ path, text }) => (text.match(/>\s*\{[^{}]*\b(?:step|item|call|tc)\.(?:tool|name)\b[^{}]*\}/g) ?? []).map((found) => `${path}: ${found}`));
    expect(shown).toEqual([]);
    expect(sources.find(({ path }) => path === "features/chat/ui/ApprovalCard.tsx")?.text.match(/\bcall\.name\b/g)).toHaveLength(1);
  });
});
