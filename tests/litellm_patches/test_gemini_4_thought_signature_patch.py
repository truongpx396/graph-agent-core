"""Regression test for the second patch in deploy/dev/litellm-patches/sitecustomize.py: a Gemini 4 model must be given the placeholder thought
signature for a tool call another model made, or Google answers 400 and the escalated turn fails.

The first test documents the bug against the STOCK litellm this project's dependencies provide (the gateway image is a floating tag: the live
proof was against its 1.94.1). The second proves the patch fixes it.
"""
import importlib.util
from pathlib import Path

import pytest

gemini = pytest.importorskip("litellm.llms.vertex_ai.gemini.vertex_and_google_ai_studio_gemini", reason="needs the litellm package this project's dependencies vendor")
factory = pytest.importorskip("litellm.litellm_core_utils.prompt_templates.factory")
ollama = pytest.importorskip("litellm.llms.ollama.chat.transformation")

PATCH_PATH = Path(__file__).resolve().parents[2] / "deploy" / "dev" / "litellm-patches" / "sitecustomize.py"
# Taken at import, before any test has loaded the patch. The file holds the Ollama patch too, whose own test needs it unapplied.
_STOCK = gemini.VertexGeminiConfig.__dict__["_is_gemini_3_or_newer"]
_STOCK_CHUNK_PARSER = ollama.OllamaChatCompletionResponseIterator.chunk_parser

# A call another model made: no signature came with it.
REPLAYED = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_x", "type": "function", "function": {"name": "search_docs", "arguments": "{}"}}]}


@pytest.fixture(autouse=True)
def _stock_litellm():
    gemini.VertexGeminiConfig._is_gemini_3_or_newer = _STOCK
    yield
    gemini.VertexGeminiConfig._is_gemini_3_or_newer = _STOCK
    ollama.OllamaChatCompletionResponseIterator.chunk_parser = _STOCK_CHUNK_PARSER


def _signed(model: str) -> bool:
    return "thoughtSignature" in factory.convert_to_gemini_tool_call_invoke(REPLAYED, model=model)[0]


def _load_patch_module():
    spec = importlib.util.spec_from_file_location("_gemini_4_thought_signature_patch", PATCH_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def test_stock_litellm_sends_a_gemini_4_model_a_replayed_call_unsigned():
    assert _signed("gemini-3.6-flash") and not _signed("gemini-4-hydrogen")


def test_the_patch_signs_it_and_leaves_other_models_as_they_were():
    _load_patch_module()

    assert _signed("gemini-4-hydrogen") and _signed("gemini-3.6-flash")
    assert not _signed("gemini-2.5-flash")
