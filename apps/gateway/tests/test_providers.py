"""Adapter translation tests — OpenAI in, provider out, OpenAI back."""

from __future__ import annotations

from src.providers.base import StreamState
from src.providers.registry import get_adapter, known_providers

OPENAI_REQUEST = {
    "model": "gpt-4o",
    "messages": [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "hello"},
    ],
    "temperature": 0.2,
}


def test_registry_has_all_three():
    assert known_providers() == ["anthropic", "google", "openai"]
    assert get_adapter("OpenAI") is not None  # case-insensitive
    assert get_adapter("nope") is None


# --- OpenAI ------------------------------------------------------------------


def test_openai_is_passthrough():
    adapter = get_adapter("openai")
    built = adapter.build_request("v1/chat/completions", OPENAI_REQUEST, "sk-test")
    assert built.url == "https://api.openai.com/v1/chat/completions"
    assert built.payload["messages"] == OPENAI_REQUEST["messages"]
    assert built.headers["authorization"] == "Bearer sk-test"


def test_openai_forces_usage_on_streams():
    """Without this, every streamed request would log as costing nothing."""
    adapter = get_adapter("openai")
    built = adapter.build_request(
        "v1/chat/completions", {**OPENAI_REQUEST, "stream": True}, "sk-test"
    )
    assert built.payload["stream_options"]["include_usage"] is True


def test_model_prefix_is_stripped():
    adapter = get_adapter("openai")
    built = adapter.build_request(
        "v1/chat/completions", {**OPENAI_REQUEST, "model": "openai/gpt-4o"}, "sk-test"
    )
    assert built.payload["model"] == "gpt-4o"


# --- Anthropic ---------------------------------------------------------------


def test_anthropic_splits_system_prompt():
    adapter = get_adapter("anthropic")
    built = adapter.build_request("v1/chat/completions", OPENAI_REQUEST, "sk-ant")

    assert built.url == "https://api.anthropic.com/v1/messages"
    assert built.payload["system"] == "You are terse."
    assert built.payload["messages"] == [{"role": "user", "content": "hello"}]
    assert built.headers["x-api-key"] == "sk-ant"
    assert "authorization" not in built.headers


def test_anthropic_supplies_required_max_tokens():
    adapter = get_adapter("anthropic")
    built = adapter.build_request("v1/chat/completions", OPENAI_REQUEST, "sk-ant")
    assert built.payload["max_tokens"] > 0


def test_anthropic_response_becomes_openai_shape():
    adapter = get_adapter("anthropic")
    openai_shaped = adapter.parse_response(
        {
            "id": "msg_1",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "hi there"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }
    )
    assert openai_shaped["object"] == "chat.completion"
    assert openai_shaped["choices"][0]["message"]["content"] == "hi there"
    assert openai_shaped["choices"][0]["finish_reason"] == "stop"
    assert openai_shaped["usage"]["prompt_tokens"] == 10
    assert openai_shaped["usage"]["total_tokens"] == 14


def test_anthropic_stream_accumulates_usage_across_events():
    adapter = get_adapter("anthropic")
    state = StreamState()

    adapter.parse_stream_chunk(
        "message_start",
        {"type": "message_start", "message": {"model": "claude-sonnet-4-6",
                                              "usage": {"input_tokens": 50, "output_tokens": 0}}},
        state,
    )
    chunk = adapter.parse_stream_chunk(
        "content_block_delta",
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
        state,
    )
    adapter.parse_stream_chunk(
        "message_delta",
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 25}},
        state,
    )

    assert chunk["choices"][0]["delta"]["content"] == "hi"
    assert chunk["object"] == "chat.completion.chunk"
    assert (state.usage.input_tokens, state.usage.output_tokens) == (50, 25)


def test_anthropic_drops_events_with_no_openai_equivalent():
    adapter = get_adapter("anthropic")
    assert adapter.parse_stream_chunk("ping", {"type": "ping"}, StreamState()) is None


# --- Google ------------------------------------------------------------------


def test_google_translates_to_generate_content():
    adapter = get_adapter("google")
    built = adapter.build_request("v1/chat/completions", OPENAI_REQUEST, "goog-key")

    assert "generateContent" in built.url
    assert built.payload["systemInstruction"]["parts"][0]["text"] == "You are terse."
    assert built.payload["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]
    assert built.payload["generationConfig"]["temperature"] == 0.2
    # Key goes in a header, never the URL, so it stays out of access logs.
    assert built.headers["x-goog-api-key"] == "goog-key"
    assert "goog-key" not in built.url


def test_google_assistant_role_becomes_model():
    adapter = get_adapter("google")
    built = adapter.build_request(
        "v1/chat/completions",
        {"model": "gemini-2.5-flash", "messages": [{"role": "assistant", "content": "prior"}]},
        "k",
    )
    assert built.payload["contents"][0]["role"] == "model"


def test_google_stream_uses_sse_endpoint():
    adapter = get_adapter("google")
    built = adapter.build_request(
        "v1/chat/completions", {**OPENAI_REQUEST, "stream": True}, "k"
    )
    assert "streamGenerateContent" in built.url
    assert "alt=sse" in built.url


def test_google_response_becomes_openai_shape():
    adapter = get_adapter("google")
    openai_shaped = adapter.parse_response(
        {
            "candidates": [
                {"content": {"parts": [{"text": "hello back"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"promptTokenCount": 6, "candidatesTokenCount": 2},
            "modelVersion": "gemini-2.5-flash",
        }
    )
    assert openai_shaped["choices"][0]["message"]["content"] == "hello back"
    assert openai_shaped["choices"][0]["finish_reason"] == "stop"
    assert openai_shaped["usage"]["completion_tokens"] == 2
