"""`drop_citation_markers`, the repair for a marker whose every citing sentence shares nothing with its source (docs/GRAPH_PATTERNS.md pattern 104).

Checked on the text it returns: the sentences stay exactly as written, only the marker and the gap it would leave go.
"""
import pytest

from app.agent.graph.citations import drop_all_citation_markers, drop_citation_markers


@pytest.mark.parametrize(
    ("text", "markers", "expected"),
    [
        # The space before the marker goes with it, so no gap is left before the full stop.
        ("Support is free [1].", ["[1]"], "Support is free."),
        # A marker written after the full stop (the other way models write it).
        ("Support is free. [1]", ["[1]"], "Support is free."),
        # In the middle of a sentence.
        ("Support [2] is free.", ["[2]"], "Support is free."),
        # A marker that opens a line takes the space AFTER it (how a model lists a source), not a leading gap.
        ("[1] Support is free.\n[2] Hours are 9 to 5.", ["[1]"], "Support is free.\n[2] Hours are 9 to 5."),
        # Several markers on one sentence: only the named ones go.
        ("Support is free [1][2].", ["[2]"], "Support is free [1]."),
        ("Support is free [1][2].", ["[1]", "[2]"], "Support is free."),
        # `[1]` is not `[10]`.
        ("Free [10] and open [1].", ["[1]"], "Free [10] and open."),
        # Nothing to drop: returned as it was.
        ("Support is free.", ["[3]"], "Support is free."),
        ("Support is free [1].", [], "Support is free [1]."),
        ("", ["[1]"], ""),
    ],
)
def test_it_removes_only_the_named_markers_and_the_gap_they_leave(text, markers, expected):
    assert drop_citation_markers(text, markers) == expected


def test_it_leaves_other_whitespace_alone():
    """A list item's indentation and a blank line between paragraphs are the answer's layout, not the marker's gap."""
    text = "Intro [1].\n\n  - first point\n  - second point [1]."
    assert drop_citation_markers(text, ["[1]"]) == "Intro.\n\n  - first point\n  - second point."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Use `items[1]` for the second one [1].", "Use `items[1]` for the second one."),
        ("```py\nfirst = rows[1]\n```\nThat reads one row [1].", "```py\nfirst = rows[1]\n```\nThat reads one row."),
        ("An unclosed fence is code to the end:\n```\nrows[1]", "An unclosed fence is code to the end:\n```\nrows[1]"),
    ],
)
def test_an_index_inside_code_is_not_a_citation(text, expected):
    """`items[1]` was removed as if it cited source 1, which changes what the code does."""
    assert drop_citation_markers(text, ["[1]"]) == expected


def test_dropping_every_marker_leaves_code_alone_too():
    assert drop_all_citation_markers("Take `a[0]` [1][2].") == "Take `a[0]`."
