"""Hand-rolled agent runtime over Anthropic tool use.

Not LangChain, not LangGraph. The loop is ~200 lines and we own every byte of it, which
buys three things that matter for this submission:

* the stored reasoning trace is exactly what we decide it is, not a framework's idea of
  a callback;
* there is no hidden prompt, no hidden retry and no hidden tool schema to explain to a
  judge;
* it degrades. With no ``ANTHROPIC_API_KEY`` the same loop runs against a deterministic
  scripted client that executes the planner's tool sequence and composes the answer from
  templates. The tools, the evidence, the verdict and the trace are identical; only the
  prose is poorer. A live demo cannot be lost to an API outage.

Two hard rules the loop enforces on the model, in code:

1. The model may call tools and write prose. It may not introduce a number. Every value
   it is shown arrives as an ``Observation`` with a ``Provenance``, and
   :func:`check_unsourced_numbers` audits the final text against that evidence.
2. A specialist sees only its own restricted tool subset. Restriction is what makes the
   collaboration real rather than five boxes on a slide.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

import httpx

from ..config import env
from ..models import Observation, ToolResult, TraceStep, utcnow
from ..store.traces import TraceStore, digest, new_step
from ..tools.registry import ToolRegistry, registry as default_registry

DEFAULT_MODEL = "claude-sonnet-4-5"
#: meta/llama-3.1-8b-instruct: confirmed function-calling support, fastest tool-calling
#: model in the free NIM catalogue at 8B — picked for testing-phase latency over quality.
#: Swap to a bigger NIM model (e.g. qwen/qwen2.5-72b-instruct) if Tamil prose quality
#: matters more than turnaround during a test run; 8B is not an officially-listed
#: Tamil-fluent model.
DEFAULT_NVIDIA_MODEL = "meta/llama-3.1-8b-instruct"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

#: Google's Gemini, reached through its **OpenAI-compatible** surface rather than the
#: native `generativelanguage` REST shape. Same reason the NIM client exists: the wire
#: format is already implemented here, so a second provider costs a base URL and a key
#: rather than a second adapter to keep in step with the first.
#: gemini-2.5-flash is retired for keys issued after its deprecation — the API returns
#: 404 "no longer available to new users" and names 3.6 as the replacement — so the
#: default is the current Flash. `gemini-flash-latest` also resolves, but pinning an
#: exact version keeps a demo reproducible when Google rolls the alias.
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

MAX_TURNS = 8


# --------------------------------------------------------------------------------------
# LLM clients
# --------------------------------------------------------------------------------------


@dataclass
class LLMTurn:
    """One assistant turn: prose plus any tool calls it wants executed."""

    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "end_turn"
    raw: Any = None


class LLMClient:
    """Interface both the real and the scripted clients satisfy."""

    available: bool = False
    name: str = "none"

    def turn(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        #: Called with each assistant text delta as it arrives, when the client and the
        #: caller both support it. Presentation only — the returned ``LLMTurn.text`` is
        #: still the authoritative full text, and a client that cannot stream simply
        #: never calls it. Nothing may depend on having received deltas.
        on_token: Callable[[str], None] | None = None,
    ) -> LLMTurn:
        raise NotImplementedError


class AnthropicClient(LLMClient):
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.model = model or env("FORESHORE_LLM_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL
        self._key = api_key or env("ANTHROPIC_API_KEY")
        self._client = None
        self.name = f"anthropic:{self.model}"
        if self._key:
            try:
                import anthropic

                self._client = anthropic.Anthropic(api_key=self._key)
                self.available = True
            except Exception:
                self._client = None
                self.available = False

    def turn(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        on_token: Callable[[str], None] | None = None,
    ) -> LLMTurn:
        # Not streamed. Accepted and ignored so the runtime can pass it unconditionally;
        # the caller's contract is that deltas are optional. Worth having if the
        # production path ever wants them — the SDK supports it — but the streaming
        # surface is a console nicety and this client is the one that must not break.
        del on_token
        if not self._client:
            raise RuntimeError("Anthropic client unavailable (no ANTHROPIC_API_KEY)")
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        resp = self._client.messages.create(**kwargs)
        text_parts: list[str] = []
        calls: list[dict[str, Any]] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append({"id": block.id, "name": block.name, "input": dict(block.input)})
        return LLMTurn(
            text="\n".join(text_parts).strip(),
            tool_calls=calls,
            stop_reason=resp.stop_reason or "end_turn",
            raw=resp,
        )


#: Bounded, and deliberately short: one agent turn is several calls, and a long sleep
#: would turn a rate limit into a demo that looks hung.
_RETRY_AFTER_DEFAULT_S = 3.0
_RETRY_AFTER_MAX_S = 20.0

#: Statuses worth trying again. A rate limit and a gateway hiccup are transient; a 400
#: (bad schema) or a 404 (retired model) will fail identically forever and retrying one
#: only makes the answer slower.
_TRANSIENT_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Attempts per model call, total. Every query is meant to go through the model, and the
#: deterministic path is the safety net rather than the plan — but the net has to be
#: reachable inside a demo's patience, so this is small and the backoff is capped.
DEFAULT_LLM_ATTEMPTS = 3


def _llm_attempts() -> int:
    """Attempts per model call. ``FORESHORE_LLM_ATTEMPTS`` overrides; clamped to 1..5 so
    a typo cannot make a demo hang."""
    raw = env("FORESHORE_LLM_ATTEMPTS")
    try:
        return min(max(int(raw), 1), 5) if raw else DEFAULT_LLM_ATTEMPTS
    except (TypeError, ValueError):
        return DEFAULT_LLM_ATTEMPTS


def _retry_after_seconds(resp: httpx.Response) -> float:
    """Honour ``Retry-After`` when the provider sends one, clamped."""
    raw = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
    try:
        return min(max(float(raw), 0.0), _RETRY_AFTER_MAX_S) if raw else _RETRY_AFTER_DEFAULT_S
    except (TypeError, ValueError):
        return _RETRY_AFTER_DEFAULT_S


#: Long enough to name the cause, short enough to render as a chip in the console's
#: "Unavailable:" row. The full body is never load-bearing — a failed model turn is
#: evidence that did not get gathered, and the answer degrades on its own.
_ERROR_CHARS = 160


def _tidy(message: str) -> str:
    """One line, capped. These strings surface in the console's `missing` list."""
    flat = " ".join(str(message).split())
    return flat if len(flat) <= _ERROR_CHARS else flat[: _ERROR_CHARS - 1].rstrip() + "…"


def _provider_error(resp: httpx.Response) -> str:
    """The provider's own error message, dug out of whichever envelope it used.

    Gemini returns a *list* containing the error object; OpenAI and NIM return the object
    directly. Anything unrecognised degrades to the truncated body rather than to a
    parse failure inside an error path.
    """
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return _tidy(resp.text or "")
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return _tidy(err.get("message") or err)
        if err:
            return _tidy(err)
    return _tidy(data)


class OpenAICompatibleClient(LLMClient):
    """One adapter for every provider that speaks the OpenAI chat-completions shape.

    NVIDIA NIM and Google Gemini both publish an OpenAI-compatible endpoint, so neither
    is Anthropic-shaped — this class is the adapter, not a copy of
    :class:`AnthropicClient`. Same interface (``system``, Anthropic-shaped ``messages``
    in, one :class:`LLMTurn` out) so :class:`AgentRuntime` does not know which wire
    format is underneath, and adding a provider costs a base URL and a key env var
    rather than a second conversion to keep in step with the first.

    Subclasses supply ``base_url``, the key env var, the default model and — in
    :meth:`extra_payload` — anything provider-specific.
    """

    base_url: str = ""
    key_env: str = ""
    fallback_key_env: str | None = None
    default_model: str = ""
    provider: str = "openai-compatible"
    timeout_s: float = 60.0

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.model = (
            model or env("FORESHORE_LLM_MODEL", self.default_model) or self.default_model
        )
        self._key = api_key or env(self.key_env) or (
            env(self.fallback_key_env) if self.fallback_key_env else None
        )
        self.name = f"{self.provider}:{self.model}"
        self.available = bool(self._key)

    def extra_payload(self) -> dict[str, Any]:
        """Provider-specific request fields. Empty by default."""
        return {}

    def budget(self, max_tokens: int) -> int:
        """The ``max_tokens`` actually sent. Identity unless the provider spends part of
        the same budget on something the caller did not ask for."""
        return max_tokens

    def turn(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        on_token: Callable[[str], None] | None = None,
    ) -> LLMTurn:
        if not self._key:
            raise RuntimeError(
                f"{self.provider} client unavailable (no {self.key_env})"
            )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _anthropic_messages_to_openai(system, messages),
            "max_tokens": self.budget(max_tokens),
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = [_anthropic_tool_to_openai(t) for t in tools]
            payload["tool_choice"] = "auto"
        payload.update(self.extra_payload())
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}

        # Streaming is only ever used for the turn whose prose a person is watching being
        # written, and only when that turn declares no tools — a streamed tool-call turn
        # would mean reassembling partial JSON arguments across deltas for no benefit,
        # since a tool call is not something anyone reads. Everything else takes the
        # single-shot path below, unchanged.
        if on_token is not None and not tools:
            return self._stream_turn(payload, headers, on_token)

        # Every query is meant to go through the model; the deterministic path is the net,
        # not the plan. Free-tier rate limits and gateway hiccups are the normal way a
        # turn is lost, and one query makes several calls, so a bounded retry converts
        # most of them into a slower answer rather than a dropped specialist. A permanent
        # error (bad schema, retired model) is not retried — it would fail identically.
        attempts = _llm_attempts()
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = httpx.post(
                    self.base_url, headers=headers, json=payload, timeout=self.timeout_s
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = RuntimeError(f"{self.provider} transport: {type(exc).__name__}")
                if attempt == attempts:
                    raise last from exc
                time.sleep(min(_RETRY_AFTER_DEFAULT_S * attempt, _RETRY_AFTER_MAX_S))
                continue

            if resp.status_code < 400:
                break

            # The provider's own message, not an HTTPStatusError with a link to MDN. This
            # string reaches the console's `missing` list, so it has to say what actually
            # went wrong ("model X is no longer available", "quota exceeded") rather than
            # what HTTP 400 means in general.
            last = RuntimeError(
                f"{self.provider} HTTP {resp.status_code}: {_provider_error(resp)}"
            )
            if resp.status_code not in _TRANSIENT_STATUSES or attempt == attempts:
                raise last
            time.sleep(min(_retry_after_seconds(resp) * attempt, _RETRY_AFTER_MAX_S))
        else:  # pragma: no cover — the loop always breaks or raises
            raise last or RuntimeError(f"{self.provider}: no response")

        data = resp.json()
        message = data["choices"][0]["message"]
        text = (message.get("content") or "").strip()
        calls: list[dict[str, Any]] = []
        for tc in message.get("tool_calls") or []:
            fn = tc["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            # Gemini omits `id` on tool calls in its OpenAI-compat responses; the loop
            # needs one to pair the result back, so synthesise a stable local id rather
            # than letting a KeyError sink the turn.
            call_id = tc.get("id") or f"{fn['name']}_{len(calls)}"
            calls.append({"id": call_id, "name": fn["name"], "input": args})
        finish = data["choices"][0].get("finish_reason") or "stop"
        stop_reason = {
            "tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens",
        }.get(finish, finish)
        return LLMTurn(text=text, tool_calls=calls, stop_reason=stop_reason, raw=data)


    def _stream_turn(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_token: Callable[[str], None],
    ) -> LLMTurn:
        """One turn over SSE, calling ``on_token`` per text delta.

        Deliberately not retried. The single-shot path retries because a lost turn costs
        a specialist; here the caller has already begun showing text to a person, and
        replaying a stream would make the answer visibly rewrite itself. A stream that
        fails mid-flight raises, the orchestrator records it, and the deterministic
        template answer ships — the same fallback as any other model failure.
        """
        body = {**payload, "stream": True}
        text_parts: list[str] = []
        finish = "stop"
        try:
            with httpx.stream(
                "POST",
                self.base_url,
                headers={**headers, "Accept": "text/event-stream"},
                json=body,
                timeout=self.timeout_s,
            ) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise RuntimeError(
                        f"{self.provider} HTTP {resp.status_code}: {_provider_error(resp)}"
                    )
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue          # a keep-alive or a frame we don't model
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    finish = choices[0].get("finish_reason") or finish
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        text_parts.append(delta)
                        try:
                            on_token(delta)
                        except Exception:  # noqa: BLE001
                            # A disconnected viewer must not fail the answer. The full
                            # text is still assembled and still audited below.
                            pass
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RuntimeError(f"{self.provider} transport: {type(exc).__name__}") from exc

        stop_reason = {
            "tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens",
        }.get(finish, finish)
        return LLMTurn(text="".join(text_parts).strip(), tool_calls=[], stop_reason=stop_reason)


class NvidiaNimClient(OpenAICompatibleClient):
    """NVIDIA NIM (build.nvidia.com), free tier, for testing without Anthropic spend."""

    base_url = NVIDIA_BASE_URL
    key_env = "NVIDIA_API_KEY"
    default_model = DEFAULT_NVIDIA_MODEL
    provider = "nvidia"

    def extra_payload(self) -> dict[str, Any]:
        if self.model.startswith("nvidia/nemotron"):
            # Nemotron reasoning models emit a "thinking" trace before the answer by
            # default — extra latency and tokens per turn we don't want on the tool-call
            # hot path. Off switch is a chat-template flag, not a normal API parameter.
            return {"chat_template_kwargs": {"enable_thinking": False}}
        return {}


class GeminiClient(OpenAICompatibleClient):
    """Google Gemini via its OpenAI-compatible endpoint.

    ``GEMINI_API_KEY`` is the documented name; ``GOOGLE_API_KEY`` is accepted as a
    fallback because that is what the Google SDKs read and it is the one people already
    have exported.

    Gemini models think before answering, and spend those tokens out of the same
    ``max_tokens`` the prose comes from. ``FORESHORE_GEMINI_REASONING`` maps straight
    onto the endpoint's ``reasoning_effort``; verified accepted on gemini-3.6-flash:
    ``minimal`` | ``low`` | ``medium`` | ``high``. ``none`` is rejected with a 400 by
    3.6-flash, so it is not the default. Unset means *send nothing* and take the model's
    own default — a field a future API version rejects must not be able to break every
    turn for a latency tweak nobody asked for.
    """

    base_url = GEMINI_BASE_URL
    key_env = "GEMINI_API_KEY"
    fallback_key_env = "GOOGLE_API_KEY"
    default_model = DEFAULT_GEMINI_MODEL
    provider = "gemini"

    #: Thinking allowance added on top of the caller's prose budget. Gemini spends
    #: thinking tokens out of ``max_tokens``, so a 1200-token synthesis budget was
    #: producing answers cut off mid-number ("... FB-05 at 12.3.") — the model had spent
    #: the budget reasoning before it wrote. The caller asks for prose room; this makes
    #: sure that is what it gets.
    THINKING_ALLOWANCE = 4096

    def _effort(self) -> str:
        return (env("FORESHORE_GEMINI_REASONING", "") or "").strip().lower()

    def extra_payload(self) -> dict[str, Any]:
        effort = self._effort()
        return {"reasoning_effort": effort} if effort else {}

    def budget(self, max_tokens: int) -> int:
        return max_tokens if self._effort() == "none" else max_tokens + self.THINKING_ALLOWANCE


#: JSON-Schema keywords the OpenAI-compatible providers accept in a function's
#: ``parameters``. Gemini validates this strictly and 400s on anything else, so the
#: schema is filtered rather than passed through — and filtering for the strictest
#: provider is harmless for the laxest one.
_ALLOWED_SCHEMA_KEYS: frozenset[str] = frozenset(
    {
        "type", "description", "properties", "required", "items", "enum",
        "minimum", "maximum", "minItems", "maxItems", "format", "nullable",
    }
)


def _sanitise_schema(node: Any) -> Any:
    """Recursively drop schema keywords the strictest provider rejects.

    Also drops an empty ``required: []`` — legal JSON Schema, and rejected by some
    Gemini API versions.
    """
    if isinstance(node, list):
        return [_sanitise_schema(v) for v in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: _sanitise_schema(v) for k, v in value.items()}
        elif key == "required":
            if value:
                out[key] = list(value)
        elif key in _ALLOWED_SCHEMA_KEYS:
            out[key] = _sanitise_schema(value)
    return out


def _anthropic_tool_to_openai(tool: dict[str, Any]) -> dict[str, Any]:
    parameters = _sanitise_schema(tool.get("input_schema") or {})
    # `type: "object"` and a `properties` map are both mandatory on the OpenAI-compatible
    # endpoints, and Gemini 400s without them. A registered tool that declares no
    # arguments legitimately has an empty `properties` — that is a complete schema, and
    # backfilling the two required keys here means one omission in one tool definition
    # cannot take down every turn for every provider.
    parameters.setdefault("type", "object")
    parameters.setdefault("properties", {})
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": parameters,
        },
    }


def _anthropic_messages_to_openai(
    system: str, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Convert the Anthropic-block message list the loop builds into OpenAI chat form.

    The loop only ever produces three shapes: a plain-string user turn, an assistant
    turn of ``text``/``tool_use`` blocks, and a user turn of ``tool_result`` blocks. OpenAI
    has no grouped tool-result message — each becomes its own ``role: tool`` message.
    """
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text = "\n".join(b["text"] for b in content if b.get("type") == "text")
            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            # Empty string, not null: an assistant turn that was pure tool calls has no
            # prose, and Gemini's OpenAI-compat layer rejects a null `content`.
            entry: dict[str, Any] = {"role": "assistant", "content": text or ""}
            if tool_uses:
                entry["tool_calls"] = [
                    {
                        "id": b["id"],
                        "type": "function",
                        "function": {"name": b["name"], "arguments": json.dumps(b["input"])},
                    }
                    for b in tool_uses
                ]
            out.append(entry)
        else:
            for b in content:
                if b.get("type") == "tool_result":
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": b["tool_use_id"],
                            "content": b.get("content") or "",
                        }
                    )
                elif b.get("type") == "text":
                    out.append({"role": "user", "content": b["text"]})
    return out


class ScriptedClient(LLMClient):
    """Deterministic stand-in used when no API key is configured.

    It is not a mock: it runs the same loop, calls the same tools with arguments the
    planner supplied, and returns the same shapes. It simply does not write prose — the
    synthesis layer templates the answer instead. Everything a judge is shown (evidence,
    verdict, trace, route, alerts) is produced by the same code path either way.
    """

    available = True
    name = "scripted"

    def __init__(self, script: Sequence[dict[str, Any]] | None = None):
        self.script = list(script or [])
        self._i = 0

    def turn(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        on_token: Callable[[str], None] | None = None,
    ) -> LLMTurn:
        del on_token          # nothing to stream: the scripted client writes no prose
        if self._i < len(self.script):
            step = self.script[self._i]
            self._i += 1
            return LLMTurn(
                text=step.get("text", ""),
                tool_calls=[
                    {
                        "id": f"scripted_{self._i}_{j}",
                        "name": c["name"],
                        "input": c.get("input", {}),
                    }
                    for j, c in enumerate(step.get("tool_calls", []))
                ],
                stop_reason="tool_use" if step.get("tool_calls") else "end_turn",
            )
        return LLMTurn(text="", tool_calls=[], stop_reason="end_turn")


#: ``FORESHORE_LLM_PROVIDER`` value -> client class. Add a provider here and it is live;
#: an unknown value falls back to Anthropic, which then falls back to ScriptedClient if
#: it has no key. There is no configuration that can leave the system without a client.
PROVIDERS: dict[str, type[LLMClient]] = {
    "anthropic": AnthropicClient,
    "gemini": GeminiClient,
    "google": GeminiClient,
    "nvidia": NvidiaNimClient,
}


def make_client(api_key: str | None = None, model: str | None = None) -> LLMClient:
    """Real client for the configured provider, scripted otherwise. Never raises.

    ``FORESHORE_LLM_PROVIDER`` picks the wire format: ``anthropic`` (default, native
    shape), ``gemini`` or ``nvidia`` (both OpenAI-compatible, one shared adapter).
    Missing/invalid key for the selected provider degrades to :class:`ScriptedClient`,
    same as before — a live demo cannot die on a missing key or dead endpoint.
    """
    provider = (env("FORESHORE_LLM_PROVIDER", "anthropic") or "anthropic").strip().lower()
    factory = PROVIDERS.get(provider, AnthropicClient)
    client = factory(api_key=api_key, model=model)
    return client if client.available else ScriptedClient()


# --------------------------------------------------------------------------------------
# Unsourced-number guard
# --------------------------------------------------------------------------------------

_NUMBER = re.compile(r"(?<![\w.])(\d{1,4}(?:[.,]\d{1,3})?)(?![\w])")

#: Public alias. The synthesis layer's polish pass compares the numeric tokens of a
#: rewrite against the tokens of the text it was given, and it must tokenise numbers
#: exactly the way this module's evidence audit does — two regexes that drift apart would
#: mean a number this guard considers "already present" and the audit does not.
NUMBER_TOKEN_RE = _NUMBER

#: Numbers that are never data: times, dates, phone numbers, list ordinals, years.
_ALLOWED_LITERALS = {
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "12", "24", "100",
    "1554", "1974", "1976", "2026",
}


def check_unsourced_numbers(
    text: str, evidence: Iterable[Observation], *, tolerance: float = 0.06
) -> list[str]:
    """Numbers in ``text`` that match no observation. Invariant 3, audited.

    A returned non-empty list is a failure, not a warning: the synthesis layer strips or
    regenerates rather than shipping a number the system cannot source.
    """
    sourced: list[float] = []
    for obs in evidence:
        if obs.is_numeric:
            v = float(obs.value)
            sourced.extend([v, round(v, 1), round(v, 2), round(v)])
            # A negative observation is written in prose with the sign carried by the
            # word, not the digits: a -1.407 degC/decade slope becomes "cooling at 1.41
            # degC/decade". Without the magnitude here that sentence failed the audit
            # (|1.41 - -1.41| = 2.82) and a correct, sourced answer was thrown away for
            # the template. Magnitudes only — this widens what counts as sourced, it
            # does not let an unsourced number through.
            a = abs(v)
            sourced.extend([a, round(a, 1), round(a, 2), round(a)])
        for q in obs.qualifiers.values():
            if isinstance(q, (int, float)) and not isinstance(q, bool):
                sourced.append(float(q))
                sourced.append(abs(float(q)))

    bad: list[str] = []
    for m in _NUMBER.finditer(text or ""):
        token = m.group(1)
        if token in _ALLOWED_LITERALS:
            continue
        # Skip clock times (07:30) and dates (2026-08-31).
        start, end = m.start(1), m.end(1)
        if start > 0 and (text[start - 1] in ":-/") or (end < len(text) and text[end] in ":-/"):
            continue
        try:
            value = float(token.replace(",", "."))
        except ValueError:
            continue
        if any(abs(value - s) <= max(tolerance, abs(s) * tolerance) for s in sourced):
            continue
        bad.append(token)
    return bad


# --------------------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------------------


@dataclass
class RunResult:
    agent: str
    text: str
    observations: list[Observation] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    steps: list[TraceStep] = field(default_factory=list)
    turns: int = 0
    stopped: str = "end_turn"
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def observations_for(self, variable: str) -> list[Observation]:
        return [o for o in self.observations if o.variable == variable]


class AgentRuntime:
    """Runs one agent: submit schemas, execute calls, feed results back, stop on answer."""

    def __init__(
        self,
        *,
        registry: ToolRegistry | None = None,
        traces: TraceStore | None = None,
        client: LLMClient | None = None,
        query_id: str = "adhoc",
        max_turns: int = MAX_TURNS,
    ) -> None:
        self.registry = registry or default_registry
        self.traces = traces or TraceStore()
        self.client = client or make_client()
        self.query_id = query_id
        self.max_turns = max_turns

    # -- helpers -----------------------------------------------------------------------

    def _record(self, step: TraceStep, sink: list[TraceStep]) -> TraceStep:
        sink.append(step)
        try:
            self.traces.append(step)
        except Exception:
            pass          # a trace-store failure must never break an advisory
        return step

    def execute_tool(
        self,
        name: str,
        args: dict[str, Any],
        *,
        agent: str,
        parent_id: str | None,
        sink: list[TraceStep],
        why: str | None = None,
    ) -> ToolResult:
        t0 = time.perf_counter()
        call_step = self._record(
            new_step(
                self.query_id, agent, "tool_call", tool=name, args=args,
                parent_id=parent_id, why=why,
            ),
            sink,
        )
        result = self.registry.call(name, args)
        self._record(
            new_step(
                self.query_id, agent, "tool_result", tool=name, args=args,
                parent_id=call_step.step_id,
                result_digest=digest(result.summary or result.payload),
                provenance_ids=result.provenance_ids,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                ok=result.ok, error=result.error,
            ),
            sink,
        )
        return result

    # -- the loop ----------------------------------------------------------------------

    def run(
        self,
        agent: str,
        system: str,
        user_message: str,
        *,
        tool_names: Sequence[str] | None = None,
        parent_id: str | None = None,
        max_tokens: int = 2048,
        max_turns: int | None = None,
        prefill_results: Sequence[tuple[str, dict[str, Any], str | None]] = (),
        prior_results: Sequence[ToolResult] = (),
        on_token: Callable[[str], None] | None = None,
    ) -> RunResult:
        """Run one agent to a final answer.

        ``prefill_results`` lets a planner hand a specialist a fixed tool sequence to
        **execute** here. ``prior_results`` is the other half of that: tool results the
        caller has *already* executed, seeded into the context without running anything
        again.

        The orchestrator needs the second. It runs every planned tool up front, then told
        each specialist "the results of those calls are already in your context" — and
        passed nothing, so they were not. The specialist found an empty context and
        re-fetched its own tools, which is where the extra model round-trips per
        specialist came from: the prompt was writing a cheque the call did not honour.

        ``max_turns`` overrides the runtime default for this call. With evidence properly
        seeded a specialist should answer in one turn; the cap leaves room for one
        genuine follow-up call and stops a model that keeps re-reading the same tool from
        spending a demo's patience on it.

        ``on_token`` streams assistant text deltas as they arrive, for the surface that
        wants to show the answer being written. It is presentation only: the returned
        ``RunResult.text`` is still the authoritative full text, and every deterministic
        guard runs on that, after.
        """
        names = list(tool_names) if tool_names is not None else self.registry.names()
        schemas = self.registry.schemas(names)
        steps: list[TraceStep] = []
        observations: list[Observation] = []
        results: list[ToolResult] = list(prior_results)
        observations.extend(o for r in prior_results for o in r.observations)
        turn_budget = self.max_turns if max_turns is None else max(1, max_turns)

        for tool_name, args, why in prefill_results:
            if tool_name not in self.registry:
                continue
            res = self.execute_tool(
                tool_name, dict(args), agent=agent, parent_id=parent_id, sink=steps, why=why
            )
            results.append(res)
            observations.extend(res.observations)

        if not self.client.available or isinstance(self.client, ScriptedClient):
            return RunResult(
                agent=agent, text="", observations=observations, tool_results=results,
                steps=steps, turns=0, stopped="scripted",
            )

        evidence_note = _evidence_block(results)
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"{user_message}\n\n{evidence_note}" if evidence_note else user_message
                ),
            }
        ]

        turns = 0
        text = ""
        error: str | None = None
        stopped = "end_turn"

        while turns < turn_budget:
            turns += 1
            try:
                # Passed only when a caller actually wants deltas, so a client that
                # predates the parameter — a test double, or anything duck-typed onto
                # LLMClient — keeps working. Streaming is a console nicety; the loop's
                # compatibility with any client is the thing that must not regress.
                extra = {"on_token": on_token} if on_token is not None else {}
                turn = self.client.turn(
                    system, messages, schemas, max_tokens=max_tokens, **extra
                )
            except Exception as exc:  # noqa: BLE001 — an LLM outage must degrade, not crash
                error = f"{type(exc).__name__}: {exc}"
                self._record(
                    new_step(
                        self.query_id, agent, "error", parent_id=parent_id,
                        ok=False, error=error, result_digest="llm turn failed",
                    ),
                    steps,
                )
                stopped = "llm_error"
                break

            if turn.text:
                text = turn.text
            if not turn.tool_calls:
                stopped = turn.stop_reason
                break

            assistant_content: list[dict[str, Any]] = []
            if turn.text:
                assistant_content.append({"type": "text", "text": turn.text})
            for call in turn.tool_calls:
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call["input"],
                    }
                )
            messages.append({"role": "assistant", "content": assistant_content})

            tool_content: list[dict[str, Any]] = []
            for call in turn.tool_calls:
                if call["name"] not in names:
                    tool_content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call["id"],
                            "is_error": True,
                            "content": (
                                f"{call['name']} is not available to {agent}. "
                                f"Available: {', '.join(names)}"
                            ),
                        }
                    )
                    continue
                res = self.execute_tool(
                    call["name"], call["input"], agent=agent,
                    parent_id=parent_id, sink=steps,
                )
                results.append(res)
                observations.extend(res.observations)
                tool_content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "is_error": not res.ok,
                        "content": _tool_result_text(res),
                    }
                )
            messages.append({"role": "user", "content": tool_content})

        return RunResult(
            agent=agent, text=text.strip(), observations=observations, tool_results=results,
            steps=steps, turns=turns, stopped=stopped, error=error,
        )


def _tool_result_text(result: ToolResult, max_obs: int = 40) -> str:
    """What the model is allowed to see: a summary plus sourced observations only.

    The full payload (geometry, long series) stays out of the context window on purpose —
    the model never needs it, and every number it does see is attached to a source.
    """
    lines: list[str] = []
    if result.summary:
        lines.append(result.summary)
    if result.error:
        lines.append(f"ERROR: {result.error}")
    if result.missing:
        lines.append(f"MISSING INPUTS: {', '.join(result.missing)}")
    for obs in result.observations[:max_obs]:
        p = obs.provenance
        res = f", {p.spatial_resolution_m/1000:.0f} km" if p.spatial_resolution_m else ""
        lines.append(
            f"- {obs.variable} = {obs.display()} at {obs.valid_time.isoformat()} "
            f"[{p.source_name} / {p.authority}{res}, {p.freshness}"
            + (", DERIVED — not an official advisory" if p.is_derived else "")
            + "]"
        )
    if len(result.observations) > max_obs:
        lines.append(f"... and {len(result.observations) - max_obs} more observations")
    for key in ("summary_payload", "nearest", "route", "geofences", "disagreements"):
        if key in result.payload:
            lines.append(f"{key}: {digest(result.payload[key])}")
    return "\n".join(lines) or "(no data)"


def _evidence_block(results: Sequence[ToolResult]) -> str:
    if not results:
        return ""
    body = "\n\n".join(f"[{r.tool}]\n{_tool_result_text(r)}" for r in results)
    return (
        "Evidence already gathered for you. Every number you may use appears below, each "
        "with its source. You must not state any quantity that is not in this list, and "
        "you must not convert or re-derive one.\n\n" + body
    )


__all__ = [
    "AgentRuntime", "RunResult", "LLMClient", "LLMTurn", "AnthropicClient",
    "OpenAICompatibleClient", "NvidiaNimClient", "GeminiClient", "PROVIDERS",
    "ScriptedClient", "make_client", "check_unsourced_numbers", "NUMBER_TOKEN_RE",
    "DEFAULT_MODEL",
    "DEFAULT_NVIDIA_MODEL", "DEFAULT_GEMINI_MODEL", "MAX_TURNS",
]
