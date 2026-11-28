import { describe, expect, it } from "vitest";
import { assistantEntry, type Usage } from "./entry";
import { costText, tokensText, totalOf, usageText } from "./usage";

const usage = (over: Partial<Usage> = {}): Usage => ({ model: "gemini/gemini-3.5-flash-lite", route: "local", tokens: 2042, costUsd: 0.0021, calls: 2, ...over });
const turn = (over: Partial<Usage>) => assistantEntry("a", { usage: usage(over) });

describe("what a turn used", () => {
  it("is one line: model, route, tokens, cost", () => {
    expect(usageText(usage())).toBe("gemini-3.5-flash-lite · local · 2.0k tokens · $0.0021");
  });

  it("says unknown for what is unknown, never zero", () => {
    expect(usageText(usage({ tokens: null, costUsd: null, model: null, route: null }))).toBe("model unknown · tokens unknown · cost unknown");
  });

  it("says when no model was called at all", () => {
    expect(usageText(usage({ calls: 0, model: null, route: null, tokens: 0, costUsd: 0 }))).toBe("no model call");
  });

  it.each([[0, "$0"], [0.00000001, "under $0.0001"], [0.0021, "$0.0021"], [1.5, "$1.50"]])("writes %s dollars as %s", (cost, text) => {
    expect(costText(cost)).toBe(text);
  });

  it("writes small counts whole", () => {
    expect(tokensText(999)).toBe("999 tokens");
  });

  it("adds up the turns on screen, and one unknown makes the sum unknown", () => {
    expect(totalOf([turn({ tokens: 10, costUsd: 1 }), turn({ tokens: 5, costUsd: 2 })])).toEqual({ tokens: 15, costUsd: 3 });
    expect(totalOf([turn({ tokens: 10, costUsd: 1 }), turn({ tokens: 5, costUsd: null })])).toEqual({ tokens: 15, costUsd: null });
    expect(totalOf([assistantEntry("replayed")])).toBeNull();
  });
});
