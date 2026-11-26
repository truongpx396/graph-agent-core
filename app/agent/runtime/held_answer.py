"""Tokens kept back until it is known that the answer stands (docs/GRAPH_PATTERNS.md pattern 105).

`context_gap` takes back an answer that says the pre-fetched documents lack it and asks again. Streamed as usual, a person read that sentence and then
watched it be replaced. So the `agent` node announces, before its call, how far to hold (`{"type": "hold", "until": ...}`, graph/stream_events.py):

- `first_sentence`: a first answer with documents in front of it. The admission, if there is one, is its first sentence: once that sentence is complete and
  is not one, everything held goes out and the rest streams as it arrives. If it is one, the whole answer waits.
- `kept`: the second look. All of it waits until check_output has judged it, because its text may be swapped for the first answer.

What is held when a `retry` arrives is dropped: nobody saw it. A `release` (the answer stands) sends it. Anything still held when the turn ends is
replaced by what the checkpoint says the answer is.
"""
import re

from app.agent.graph.messages import split_sentences
from app.agent.graph.output_guardrails import admits_context_gap

# A sentence is over when its stop (and any marker written after it) is followed by something, or a line ends.
_SENTENCE_OVER_RE = re.compile(r"[.!?](?:[ \t]*\[\d+\])*\s|\n")
# A first sentence longer than this is judged on what has arrived: a list with no full stop must not hold the whole answer.
_MAX_UNJUDGED_CHARS = 300


class HeldAnswer:
    def __init__(self) -> None:
        self._until: str | None = None
        self._parts: list[str] = []

    @property
    def pending(self) -> bool:
        """True while text is being kept back."""
        return bool(self._parts)

    def hold(self, until: str) -> None:
        self._until, self._parts = until, []

    def feed(self, token: str) -> str:
        """What can be sent now that `token` has arrived: the token itself when nothing is held, "" while waiting."""
        if self._until is None:
            return token
        self._parts.append(token)
        if self._until != "first_sentence":
            return ""
        text = "".join(self._parts)
        if not (_SENTENCE_OVER_RE.search(text.lstrip()) or len(text) >= _MAX_UNJUDGED_CHARS):
            return ""
        return self._judge(text)

    def call_ended(self, *, called_a_tool: bool) -> str:
        """What can be sent when the model call is over. Text written beside a tool call is not an answer about to be taken back."""
        if called_a_tool:
            return self.release()
        if self._until == "first_sentence":
            return self._judge("".join(self._parts))
        return ""

    def release(self) -> str:
        text = "".join(self._parts)
        self.discard()
        return text

    def discard(self) -> None:
        self._until, self._parts = None, []

    def _judge(self, text: str) -> str:
        sentences = split_sentences(text)
        if sentences and admits_context_gap(sentences[0]):
            self._until = "kept"
            return ""
        return self.release()
