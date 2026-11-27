import type { Suggestion } from "./domains";

export interface Tag {
  text: string;
  kind: "route" | "ability";
}

/** The marks shown under a suggested question: the model tier first, then the skill, the subagents and the tools. */
export function tagsOf({ route, skill, subagents = [], tools = [] }: Suggestion): Tag[] {
  const tags: Tag[] = [];
  if (route) tags.push({ kind: "route", text: `${route} model` });
  if (skill) tags.push({ kind: "ability", text: `skill: ${skill}` });
  if (subagents.length === 1) tags.push({ kind: "ability", text: `subagent: ${subagents[0]}` });
  if (subagents.length > 1) tags.push({ kind: "ability", text: `${subagents.length} subagents: ${subagents.join(", ")}` });
  if (tools.length > 1) {
    const counted = [...new Set(tools)].map((name) => {
      const times = tools.filter((other) => other === name).length;
      return times > 1 ? `${name} ×${times}` : name;
    });
    tags.push({ kind: "ability", text: `${tools.length} tool calls: ${counted.join(", ")}` });
  }
  return tags;
}
