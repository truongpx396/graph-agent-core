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
