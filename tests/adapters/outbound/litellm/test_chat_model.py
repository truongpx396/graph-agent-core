"""`app/adapters/outbound/litellm/chat_model.py`: what the real `ChatModels` adapter builds."""
import httpx
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from app.adapters.outbound.litellm import chat_model
from app.adapters.outbound.litellm.chat_model import OpenAiChatModels
from app.core.config import REQUEST_TIMEOUT_SECONDS


@tool
def lookup(query: str) -> str:
    """Looks something up."""
    return query


def _client(model):
    return model.bound  # a RunnableBinding around the ChatOpenAI


def test_it_is_a_chat_openai_at_the_given_endpoint_with_token_usage_on_and_no_sampling():
    model = OpenAiChatModels().build([lookup], model="chat", base_url="http://gateway.test/v1", api_key="sk-test")

    client = _client(model)
    assert isinstance(client, ChatOpenAI)
    assert client.model_name == "chat" and client.openai_api_base == "http://gateway.test/v1"
    assert client.temperature == 0, "an agent's tool choice must not be a dice roll"
    assert client.stream_usage is True, "without it a streamed turn reports 0 tokens and MAX_TOKENS_PER_TURN never trips"
    assert client.openai_api_key.get_secret_value() == "sk-test"


def test_the_tools_it_is_given_are_bound():
    model = OpenAiChatModels().build([lookup], model="chat", base_url="http://gateway.test/v1", api_key="k")

    assert [t["function"]["name"] for t in model.kwargs["tools"]] == ["lookup"]


def test_the_chat_client_has_a_read_timeout_and_no_sdk_retries():
    """Real gap (langchain-openai 0.3.35, openai 2.54.0): ChatOpenAI passed no timeout, so a stalled call waited for the turn's whole
    wall-clock limit (and, outside a turn, for ever), and the SDK retried twice under the graph's three attempts under the gateway's
    two: up to 27 calls to the provider for one logical call (PRODUCTION_REVIEW.md R06)."""
    model = OpenAiChatModels().build([lookup], model="chat", base_url="http://gateway.test/v1", api_key="k")

    for sdk_client in (_client(model).root_async_client, _client(model).root_client):
        assert sdk_client.max_retries == 0
        assert isinstance(sdk_client.timeout, httpx.Timeout)
        assert sdk_client.timeout.read == float(REQUEST_TIMEOUT_SECONDS), "until an operator lowers it, nothing changes for a turn"
        assert (sdk_client.timeout.connect, sdk_client.timeout.pool) == (5.0, 5.0), "an unreachable gateway is told apart from a slow model"


def test_a_lowered_read_timeout_and_a_raised_retry_count_reach_the_client(monkeypatch):
    monkeypatch.setattr(chat_model, "LLM_READ_TIMEOUT_SECONDS", 12.5)
    monkeypatch.setattr(chat_model, "LLM_SDK_MAX_RETRIES", 1)

    client = _client(OpenAiChatModels().build([lookup], model="chat", base_url="http://gateway.test/v1", api_key="k"))

    assert client.root_async_client.timeout.read == 12.5
    assert client.root_async_client.max_retries == 1
