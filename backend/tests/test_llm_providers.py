"""Provider selection and the shared OpenAI-compatible adapter.

``FORESHORE_LLM_PROVIDER`` picks a wire format, not a capability: the loop, the tool
schemas, the trace and the safety path are identical whichever provider answers. These
tests pin the two things that are easy to break when a provider is added — that an
unknown or unkeyed provider still yields a working client rather than an exception, and
that the schema/message conversion stays inside what the strictest endpoint accepts.
"""

from __future__ import annotations

import httpx
import pytest

from foreshore.agents.runtime import (
    AnthropicClient,
    GeminiClient,
    NvidiaNimClient,
    PROVIDERS,
    ScriptedClient,
    _anthropic_messages_to_openai,
    _anthropic_tool_to_openai,
    _provider_error,
    _sanitise_schema,
    make_client,
)


# ---------------------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------------------


def test_every_provider_name_maps_to_a_client(monkeypatch: pytest.MonkeyPatch) -> None:
    assert PROVIDERS["gemini"] is GeminiClient
    assert PROVIDERS["google"] is GeminiClient
    assert PROVIDERS["nvidia"] is NvidiaNimClient
    assert PROVIDERS["anthropic"] is AnthropicClient


def test_gemini_is_selected_and_reads_its_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESHORE_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("FORESHORE_LLM_MODEL", raising=False)
    client = make_client()
    assert client.available
    assert client.name.startswith("gemini:")


def test_gemini_falls_back_to_the_google_sdk_key_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    assert GeminiClient().available


def test_a_provider_with_no_key_degrades_to_the_scripted_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live demo cannot die on a missing key — the tools, evidence, verdict and trace
    are identical on the scripted path; only the prose is poorer."""
    monkeypatch.setenv("FORESHORE_LLM_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert isinstance(make_client(), ScriptedClient)


def test_an_unknown_provider_name_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESHORE_LLM_PROVIDER", "not-a-provider")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert make_client() is not None


# ---------------------------------------------------------------------------------------
# The shared adapter
# ---------------------------------------------------------------------------------------


def test_schema_sanitiser_drops_unsupported_keywords_and_empty_required() -> None:
    """Gemini 400s on schema keywords it does not know, and on `required: []`."""
    out = _sanitise_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "$schema": "http://json-schema.org/draft-07/schema#",
            "required": [],
            "properties": {
                "lat": {"type": "number", "description": "d", "examples": [1]},
                "classes": {"type": "array", "items": {"type": "string", "enum": ["A"]}},
            },
        }
    )
    assert "additionalProperties" not in out
    assert "$schema" not in out
    assert "required" not in out
    assert "examples" not in out["properties"]["lat"]
    assert out["properties"]["classes"]["items"]["enum"] == ["A"]


def test_every_registered_tool_survives_the_sanitiser_intact() -> None:
    """The filter must not silently strip something a tool actually needs — every tool's
    declared parameters have to still be there after conversion."""
    from foreshore.tools import registry

    for spec in registry.all():
        converted = _anthropic_tool_to_openai(spec.anthropic_schema())
        params = converted["function"]["parameters"]
        assert params.get("type") == "object"
        assert set(params.get("properties", {})) == set(
            spec.input_schema.get("properties", {})
        ), spec.name


def test_a_tool_only_assistant_turn_carries_an_empty_string_not_null() -> None:
    """Gemini's OpenAI-compat layer rejects a null `content`."""
    converted = _anthropic_messages_to_openai(
        "sys",
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
            },
        ],
    )
    assistant = next(m for m in converted if m["role"] == "assistant")
    assert assistant["content"] == ""
    assert assistant["tool_calls"][0]["id"] == "t1"
    assert converted[-1] == {"role": "tool", "tool_call_id": "t1", "content": "ok"}


def test_gemini_adds_thinking_headroom_to_the_prose_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini spends thinking tokens out of `max_tokens`, which was cutting answers off
    mid-number. The caller asks for prose room and must get it."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("FORESHORE_GEMINI_REASONING", "low")
    assert GeminiClient().budget(1200) == 1200 + GeminiClient.THINKING_ALLOWANCE
    monkeypatch.setenv("FORESHORE_GEMINI_REASONING", "none")
    assert GeminiClient().budget(1200) == 1200


def test_reasoning_effort_is_only_sent_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("FORESHORE_GEMINI_REASONING", "")
    assert GeminiClient().extra_payload() == {}
    monkeypatch.setenv("FORESHORE_GEMINI_REASONING", "low")
    assert GeminiClient().extra_payload() == {"reasoning_effort": "low"}


# ---------------------------------------------------------------------------------------
# Error legibility
# ---------------------------------------------------------------------------------------


def test_provider_errors_are_read_out_of_either_envelope() -> None:
    """Gemini wraps the error object in a list; OpenAI and NIM do not. This string
    reaches the console's "Unavailable:" row, so it has to say what went wrong."""
    gemini = httpx.Response(
        429, json=[{"error": {"code": 429, "message": "You exceeded your current quota."}}]
    )
    assert _provider_error(gemini) == "You exceeded your current quota."

    openai_shaped = httpx.Response(400, json={"error": {"message": "bad model"}})
    assert _provider_error(openai_shaped) == "bad model"


def test_provider_errors_are_one_short_line() -> None:
    """It is rendered as a chip, not a log line."""
    noisy = httpx.Response(429, json={"error": {"message": "a\n  b   c " + "x" * 400}})
    out = _provider_error(noisy)
    assert "\n" not in out
    assert len(out) <= 160


def test_a_non_json_error_body_still_yields_a_string() -> None:
    assert _provider_error(httpx.Response(502, text="<html>bad gateway</html>"))
