"""What one chat request may carry (PRODUCTION_REVIEW.md R14): a message, a conversation id and attached images were unbounded, and a request body is parsed
whole, copied into a Redis stream entry and then into the checkpoint, so one large request cost memory in four places.

The limits are `Settings` values read when the schema is defined, so these read them back from `app.core.config` and test each edge on both sides.
"""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.adapters.inbound.api import main as api
from app.adapters.inbound.api.schemas import (
    THREAD_ID_MAX_LENGTH,
    CancelRequest,
    ChatRequest,
    ResumeRequest,
)
from app.core.config import (
    CHAT_IMAGE_MAX_CHARS,
    CHAT_MAX_IMAGES,
    CHAT_MESSAGE_MAX_CHARS,
)

HEADERS = {"X-Tenant-Id": "ecorp", "X-Principal-Id": "p1"}


def _refused_fields(**fields):
    with pytest.raises(ValidationError) as refused:
        ChatRequest(**{"message": "hi", **fields})
    return {error["loc"][0] for error in refused.value.errors()}


def test_a_message_at_the_limit_is_accepted_and_one_character_over_is_not():
    assert ChatRequest(message="x" * CHAT_MESSAGE_MAX_CHARS).message
    assert _refused_fields(message="x" * (CHAT_MESSAGE_MAX_CHARS + 1)) == {"message"}


def test_an_empty_message_is_still_refused():
    assert _refused_fields(message="") == {"message"}


@pytest.mark.parametrize("model", [ChatRequest, ResumeRequest, CancelRequest])
def test_a_conversation_id_is_bounded_on_every_request_that_names_one(model):
    extra = {"message": "hi"} if model is ChatRequest else {"approved": True} if model is ResumeRequest else {}

    assert model(thread_id="t" * THREAD_ID_MAX_LENGTH, **extra).thread_id
    for bad in ("", "t" * (THREAD_ID_MAX_LENGTH + 1)):
        with pytest.raises(ValidationError):
            model(thread_id=bad, **extra)


def test_a_conversation_id_has_no_character_pattern_because_a_telegram_one_embeds_a_principal():
    assert ChatRequest(message="hi", thread_id="telegram:a1b2c3d4-e5f6:99887766").thread_id


def test_a_chat_request_without_an_id_still_gets_one():
    assert len(ChatRequest(message="hi").thread_id) == 36


def test_the_number_of_images_is_bounded():
    assert len(ChatRequest(message="hi", images=["http://x/i.png"] * CHAT_MAX_IMAGES).images) == CHAT_MAX_IMAGES
    assert _refused_fields(images=["http://x/i.png"] * (CHAT_MAX_IMAGES + 1)) == {"images"}


def test_the_size_of_each_image_is_bounded():
    assert ChatRequest(message="hi", images=["x" * CHAT_IMAGE_MAX_CHARS]).images
    assert _refused_fields(images=["ok", "x" * (CHAT_IMAGE_MAX_CHARS + 1)]) == {"images"}


def test_the_web_pages_single_image_is_inside_the_limit():
    """The page attaches one image per turn; a default below one would break it."""
    assert CHAT_MAX_IMAGES >= 1


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"message": "x" * (CHAT_MESSAGE_MAX_CHARS + 1)}, "message"),
        ({"message": "hi", "thread_id": "t" * (THREAD_ID_MAX_LENGTH + 1)}, "thread_id"),
        ({"message": "hi", "images": ["x"] * (CHAT_MAX_IMAGES + 1)}, "images"),
    ],
)
def test_a_request_over_a_bound_is_a_422_that_names_the_field(body, field):
    """Through HTTP (no lifespan: `TestClient` without `with`), so a client sees the response it will really get."""
    response = TestClient(api.app).post("/chat/stream/queued", json=body, headers=HEADERS)

    assert response.status_code == 422
    assert field in {part for error in response.json()["detail"] for part in error["loc"]}
