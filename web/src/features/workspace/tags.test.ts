import { describe, expect, it } from "vitest";
import { SUGGESTED_QUESTIONS } from "./domains";
import { tagsOf } from "./tags";

const texts = (suggestion: Parameters<typeof tagsOf>[0]) => tagsOf(suggestion).map((tag) => tag.text);

describe("the marks on a suggested question", () => {
  it("shows nothing for a plain question", () => {
    expect(texts({ text: "q" })).toEqual([]);
  });

  it("names the tier, the skill, several subagents and repeated tool calls", () => {
    expect(
      texts({ text: "q", route: "strong", skill: "s", subagents: ["a", "b"], tools: ["t", "t", "u"] }),
    ).toEqual(["strong model", "skill: s", "2 subagents: a, b", "3 tool calls: t ×2, u"]);
  });

  it("does not call one tool call several", () => {
    expect(texts({ text: "q", subagents: ["a"], tools: ["t"] })).toEqual(["subagent: a"]);
  });

  it("has something to show for every domain", () => {
    const missing = Object.entries(SUGGESTED_QUESTIONS).flatMap(([domain, questions]) =>
      (["medium", "strong"] as const)
        .filter((tier) => !questions.some((question) => question.route === tier))
        .map((tier) => `${domain}: no ${tier} question`),
    );
    expect(missing).toEqual([]);
  });
});
