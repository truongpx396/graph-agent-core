"""A team-channel post is what people read: one that still holds a placeholder is refused. It was posted as "Current latency metrics: [insert details here]."."""
import pytest
from pydantic import ValidationError

from app.domains.ops.tools import PostToTeamChannelArgs


@pytest.mark.parametrize(
    "message",
    ["Current latency metrics: [insert details here].", "p95 is <insert value> seconds", "Status: {{summary}}", "Owner: [your name]"],
)
def test_a_placeholder_is_refused(message):
    with pytest.raises(ValidationError, match="placeholder"):
        PostToTeamChannelArgs(channel="ops-digest", message=message, tool_call_id="t1")


@pytest.mark.parametrize("message", ["p95 latency is 4.2s (threshold 30s).", "Incident [#12] resolved; see items[0].", "all clear"])
def test_a_written_message_is_accepted(message):
    assert PostToTeamChannelArgs(channel="ops-digest", message=message, tool_call_id="t1").message == message
