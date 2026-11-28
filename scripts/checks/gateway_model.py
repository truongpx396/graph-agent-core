"""A hosted alias, asked through the running gateway (`make up`) and answered in the shape Ollama's `/api/chat` replies in, so `probe-tools`
and `prompt-check` judge whatever really serves an alias.

A reply from the alias's fallback model is never counted. LiteLLM says so in the `x-litellm-attempted-fallbacks` header; the reply's own
`model` is the alias either way, so without the header a local model's answer would be scored as the hosted one's.
"""
import json
import time
from typing import Any

import httpx

from app.core.config import settings

# A free tier says "too many requests" or "high demand" often enough that a single attempt would measure Google's load, not the model.
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
WAITS_SECONDS: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0)


def to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ollama-shaped messages as OpenAI ones: a call gets an id and its arguments as text, and the result after it names that id."""
    out: list[dict[str, Any]] = []
    calls, last_id = 0, ""
    for message in messages:
        if message.get("tool_calls"):
            wired = []
            for call in message["tool_calls"]:
                calls, last_id = calls + 1, f"call_{calls + 1}"
                function = {"name": call["function"]["name"], "arguments": json.dumps(call["function"]["arguments"])}
                wired.append({"id": last_id, "type": "function", "function": function})
            out.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": wired})
        elif message["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": last_id, "content": message["content"]})
        else:
            out.append({"role": message["role"], "content": message["content"]})
    return out


def _arguments(text: str) -> object:
    """A call's arguments as the object they name, or the text itself when it is not JSON: the probe then calls them bad arguments."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def from_openai(body: dict[str, Any]) -> dict[str, Any]:
    choice = body["choices"][0]
    message = choice["message"]
    calls = [
        {"function": {"name": c["function"]["name"], "arguments": _arguments(c["function"]["arguments"])}} for c in message.get("tool_calls") or []
    ]
    return {
        "message": {"role": "assistant", "content": message.get("content") or "", "tool_calls": calls},
        "done_reason": choice.get("finish_reason"),
        "eval_count": (body.get("usage") or {}).get("completion_tokens", 0),
    }


def chat(client: httpx.Client, alias: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": alias, "messages": to_openai(messages), "temperature": 0}
    if tools:
        payload["tools"] = tools
    url = f"{settings.openai_api_base.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
    for wait in (*WAITS_SECONDS, None):
        try:
            reply = client.post(url, json=payload, headers=headers, timeout=120)
        except httpx.TimeoutException:
            problem = "no answer in time"
        except httpx.ConnectError as exc:
            raise SystemExit(f"no gateway at {url} ({exc}): a hosted alias is asked through it, so run `make up` first") from exc
        else:
            fell_back = reply.headers.get("x-litellm-attempted-fallbacks", "0") != "0"
            if reply.status_code == 200 and not fell_back:
                return from_openai(reply.json())
            if reply.status_code != 200 and reply.status_code not in RETRY_STATUSES:
                raise SystemExit(f"{alias!r}: HTTP {reply.status_code}: {reply.text[:200]}")
            problem = "the gateway answered with the fallback model" if fell_back else f"HTTP {reply.status_code}"
        if wait is None:
            break
        time.sleep(wait)
    raise SystemExit(f"{alias!r} did not answer in {len(WAITS_SECONDS) + 1} attempts: {problem} (the gateway's log says why)")
