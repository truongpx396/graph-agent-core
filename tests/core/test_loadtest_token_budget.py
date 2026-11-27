"""The load test's token-budget scenario sends one message meant to end a turn at MAX_TOKENS_PER_TURN. Two numbers in two other files decide whether it
can: the API's message limit and the ceiling the load-test worker runs with. It sent 34,000 characters to an API that takes 32,000, so every request was
a 422 and no turn ever reached the ceiling."""
import re
from pathlib import Path

import yaml

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def _huge_message() -> str:
    # Read, not imported: locust is not a test dependency.
    found = re.search(r'^HUGE_MESSAGE = "x " \* (\d+)$', (ROOT / "loadtest/locustfile_queued.py").read_text(encoding="utf-8"), re.MULTILINE)
    assert found, "HUGE_MESSAGE changed shape: update this test with it"
    return "x " * int(found.group(1))


def test_the_api_accepts_the_token_budget_message():
    assert len(_huge_message()) <= Settings.model_fields["chat_message_max_chars"].default


def test_the_message_comes_back_larger_than_the_load_test_workers_ceiling():
    overlay = yaml.safe_load((ROOT / "deploy/dev/loadtest.yml").read_text(encoding="utf-8"))
    ceiling = int(overlay["services"]["agent-worker"]["environment"]["MAX_TOKENS_PER_TURN"])

    assert len(_huge_message().split()) > ceiling
