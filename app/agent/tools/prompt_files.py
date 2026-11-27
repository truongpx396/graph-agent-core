"""A domain's system prompt is a text file beside its `domain.py` (`prompt.md`), like its skills and subagents (pattern 114).

A prompt is prose that is reviewed and edited as prose: inside a Python string it was wrapped, quoted and commented differently in each
domain. The file is the whole text the model is sent for that domain (the platform's rules are added by `build_graph`), so nothing in it
is a comment: the reasons for a sentence stay in `domain.py`. It is read once, at import, so the prefix stays a constant (pattern 19), and a
missing or empty file stops the process at start-up instead of leaving an agent with no instructions.
"""
from pathlib import Path


def load_prompt(path: Path, **parts: str) -> str:
    """The prompt in `path`. Each keyword fills the `{keyword}` placeholder of the same name, which the file must contain: a part that went
    nowhere would be a paragraph silently missing from the prompt."""
    text = path.read_text(encoding="utf-8").strip()
    for name, value in parts.items():
        if f"{{{name}}}" not in text:
            raise ValueError(f"{path}: no {{{name}}} placeholder to fill")
        text = text.replace(f"{{{name}}}", value)
    if not text:
        raise ValueError(f"{path}: the prompt is empty")
    return text
