"""`scripts/checks/gateway_model.py`: how the checks ask a hosted alias, and what they refuse to count."""
import json

import httpx
import pytest

from scripts.checks import gateway_model
from scripts.checks import toolcall_probe as tp

CALL = {"function": {"name": "query_employees", "arguments": {"department": "Engineering"}}}


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(gateway_model, "WAITS_SECONDS", (0.0, 0.0))


def _reply(message: dict, *, status: int = 200, fallbacks: str = "0") -> httpx.Response:
    body = {"choices": [{"message": message, "finish_reason": "stop"}], "usage": {"completion_tokens": 7}}
    return httpx.Response(status, json=body, headers={"x-litellm-attempted-fallbacks": fallbacks})


def _client(*replies: httpx.Response, seen: list | None = None) -> httpx.Client:
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((str(request.url), json.loads(request.content)))
        return queue.pop(0)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_hosted_alias_is_asked_through_the_gateway_by_name_and_answered_in_ollamas_shape():
    seen: list = []
    tool_call = {"id": "c1", "type": "function", "function": {"name": "query_employees", "arguments": '{"department": "Engineering"}'}}
    client = _client(_reply({"content": None, "tool_calls": [tool_call]}), seen=seen)

    body = tp.chat(client, None, "chat", {}, [{"role": "user", "content": "who?"}], [{"type": "function"}])

    assert body["message"] == {"role": "assistant", "content": "", "tool_calls": [CALL]}
    assert (body["eval_count"], body["done_reason"]) == (7, "stop")
    url, sent = seen[0]
    assert url.endswith("/chat/completions") and sent["model"] == "chat" and sent["temperature"] == 0 and sent["tools"]


def test_a_replayed_call_and_its_result_carry_the_same_id():
    messages = gateway_model.to_openai(
        [
            {"role": "system", "content": "s"},
            {"role": "assistant", "content": "", "tool_calls": [CALL]},
            {"role": "tool", "content": "- Priya", "tool_name": "query_employees"},
        ]
    )

    call = messages[1]["tool_calls"][0]
    assert messages[2] == {"role": "tool", "tool_call_id": call["id"], "content": "- Priya"}
    assert json.loads(call["function"]["arguments"]) == CALL["function"]["arguments"]


def test_a_busy_provider_is_asked_again():
    client = _client(_reply({}, status=503), _reply({}, status=429), _reply({"content": "hi"}))

    assert gateway_model.chat(client, "chat", [{"role": "user", "content": "x"}])["message"]["content"] == "hi"


def test_the_fallback_models_answer_is_never_counted_as_the_alias_own():
    """The gateway answers `chat` with the local model when the hosted one refuses; the reply still says `model: chat`."""
    client = _client(*[_reply({"content": "from qwen"}, fallbacks="1") for _ in range(3)])

    with pytest.raises(SystemExit, match="fallback model"):
        gateway_model.chat(client, "chat", [{"role": "user", "content": "x"}])


def test_a_refused_request_is_not_repeated():
    client = _client(_reply({}, status=400))  # one reply only: a second request would raise IndexError

    with pytest.raises(SystemExit, match="HTTP 400"):
        gateway_model.chat(client, "chat", [{"role": "user", "content": "x"}])


def test_a_penalty_override_is_refused_for_a_hosted_alias(capsys):
    with pytest.raises(SystemExit):
        tp.main(["--repeat-penalty", "1.3"])
    assert "chat-backup" in capsys.readouterr().err
