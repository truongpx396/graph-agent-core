// A rehype plugin: each word gets a span of its own, so the stylesheet can fade in the new ones. React keeps the spans it already has
// (they are keyed by position), and a CSS animation runs only when an element is created, so words on screen stay still.
interface Node {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: Node[];
}

const word = (value: string): Node =>
  /^\s+$/.test(value)
    ? { type: "text", value }
    : { type: "element", tagName: "span", properties: { className: ["fade-in"] }, children: [{ type: "text", value }] };

function wrap(node: Node): void {
  // Code is read and copied as one piece; its block fades in as a whole.
  if (!node.children || node.tagName === "code") return;
  node.children = node.children.flatMap((child) => {
    if (child.type === "text") return (child.value ?? "").split(/(\s+)/).filter(Boolean).map(word);
    wrap(child);
    return [child];
  });
}

export const fadeInWords = () => wrap;
