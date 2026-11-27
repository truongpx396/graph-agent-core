"""A domain's prompt is a text file beside it (app/agent/tools/prompt_files.py, GRAPH_PATTERNS.md pattern 114)."""
from pathlib import Path

import pytest

from app.agent.tools.prompt_files import load_prompt
from app.domains.registry import DOMAINS
from app.domains.sales.domain import SALES_FOLLOWUP_PROMPT, SALES_VOICE

DOMAINS_DIR = Path(__file__).resolve().parents[3] / "app" / "domains"


def _file(tmp_path, text):
    path = tmp_path / "prompt.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_part_fills_its_placeholder(tmp_path):
    assert load_prompt(_file(tmp_path, "You are x.\n\n{voice}\n"), voice="Be brief.") == "You are x.\n\nBe brief."


def test_a_part_with_nowhere_to_go_is_an_error_not_a_missing_paragraph(tmp_path):
    with pytest.raises(ValueError, match="voice"):
        load_prompt(_file(tmp_path, "You are x."), voice="Be brief.")


@pytest.mark.parametrize("text", ["", "   \n\n"])
def test_an_empty_prompt_stops_the_process(tmp_path, text):
    with pytest.raises(ValueError, match="empty"):
        load_prompt(_file(tmp_path, text))


def test_a_missing_file_stops_the_process(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_prompt(tmp_path / "prompt.md")


@pytest.mark.parametrize("domain", sorted(DOMAINS))
def test_every_domains_prompt_is_its_file(domain):
    """So a new domain follows the shape, and nobody edits a string in domain.py while the file says something else."""
    text = (DOMAINS_DIR / domain / "prompt.md").read_text(encoding="utf-8").strip()
    parts = {"voice": SALES_VOICE} if domain == "sales" else {}
    for name, value in parts.items():
        text = text.replace(f"{{{name}}}", value)

    assert DOMAINS[domain][0].system_prompt == text
    assert "{" not in DOMAINS[domain][0].system_prompt, "a placeholder nobody filled"


def test_the_follow_up_prompt_shares_the_voice():
    assert SALES_VOICE in SALES_FOLLOWUP_PROMPT and SALES_VOICE in DOMAINS["sales"][0].system_prompt
