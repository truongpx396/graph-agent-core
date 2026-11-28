import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Citation } from "@/shared/api";
import { assistantEntry } from "../model/entry";
import { AssistantMessage } from "./AssistantMessage";

const nothing = () => undefined;

function source(citation: Partial<Citation>) {
  const full: Citation = { marker: "[1]", doc_id: "d1", title: "Qdrant: Payload", text: "passage", score: 1, ...citation };
  const entry = assistantEntry("a1", { status: null, answer: "An answer [1].", citations: [full] });
  return render(<AssistantMessage entry={entry} onAsk={nothing} onDecide={nothing} />).container.querySelector(".citation-item")!;
}

describe("a source under an answer", () => {
  it("links its title to the original document, away from this page", () => {
    const link = source({ source_url: "https://qdrant.tech/documentation/manage-data/payload/" }).querySelector("a");
    expect(link?.textContent).toBe("Qdrant: Payload");
    expect(link?.getAttribute("href")).toBe("https://qdrant.tech/documentation/manage-data/payload/");
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(link?.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it.each([undefined, null, "javascript:alert(1)", "data:text/html,x", "/relative"])("is plain text for the address %s", (url) => {
    const item = source({ source_url: url });
    expect(item.querySelector("a")).toBeNull();
    expect(item.textContent).toContain("Qdrant: Payload");
  });
});

const message = (over: Parameters<typeof assistantEntry>[1]) => (
  <AssistantMessage entry={assistantEntry("a1", { status: null, ...over })} onAsk={nothing} onDecide={nothing} />
);

describe("an answer as it streams", () => {
  it("is rendered as markdown while it is still a draft", () => {
    const { container } = render(message({ draft: "# Title\n\n- one" }));
    expect(container.querySelector(".answer-text.draft-text h1")?.textContent).toBe("Title");
    expect(container.querySelector(".draft-text li")?.textContent).toBe("one");
  });

  it("stays the same elements when the draft is committed, so nothing flashes or fades again", () => {
    const view = render(message({ draft: "It is **42**." }));
    const strong = view.container.querySelector("strong");
    view.rerender(message({ answer: "It is **42**." }));
    expect(view.container.querySelector("strong")).toBe(strong);
    expect(view.container.querySelector(".draft-text")).toBeNull();
  });

  it("keeps what was committed before a pause when more streams after it", () => {
    const { container } = render(message({ answer: "Before.", draft: "After" }));
    expect([...container.querySelectorAll(".answer-text p")].map((node) => node.textContent)).toEqual(["Before.", "After"]);
  });

  it("does not animate a message replayed from history", () => {
    const { container } = render(message({ answer: "An old answer." }));
    expect(container.querySelector(".streamed, .fade-in")).toBeNull();
  });
});
