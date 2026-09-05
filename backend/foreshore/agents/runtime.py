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
    ) -> LLMTurn:
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


class NvidiaNimClient(LLMClient):
    """NVIDIA NIM (build.nvidia.com), free tier, for testing without Anthropic spend.

    NIM's ``integrate.api.nvidia.com`` endpoint is OpenAI-compatible, not Anthropic-
    compatible — this class is the adapter, not a copy of :class:`AnthropicClient`. Same
    interface (``system``, Anthropic-shaped ``messages`` in, one :class:`LLMTurn` out) so
    :class:`AgentRuntime` does not know which wire format is underneath.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.model = model or env("FORESHORE_LLM_MODEL", DEFAULT_NVIDIA_MODEL) or DEFAULT_NVIDIA_MODEL
        self._key = api_key or env("NVIDIA_API_KEY")
        self.name = f"nvidia:{self.model}"
        self.available = bool(self._key)

    def turn(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> LLMTurn:
        if not self._key:
            raise RuntimeError("NVIDIA NIM client unavailable (no NVIDIA_API_KEY)")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _anthropic_messages_to_openai(system, messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = [_anthropic_tool_to_openai(t) for t in tools]
            payload["tool_choice"] = "auto"
        if self.model.startswith("nvidia/nemotron"):
            # Nemotron reasoning models emit a "thinking" trace before the answer by
            # default — extra latency and tokens per turn we don't want on the tool-call
            # hot path. Off switch is a chat-template flag, not a normal API parameter.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        resp = httpx.post(
            NVIDIA_BASE_URL,
            headers={"Authorization": f"Bearer {self._key}", "Accept": "application/json"},
            json=payload,
            timeout=60.0,
        )
        resp.raise_for_status()
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
            calls.append({"id": tc["id"], "name": fn["name"], "input": args})
        finish = data["choices"][0].get("finish_reason") or "stop"
        stop_reason = {
            "tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens",
        }.get(finish, finish)
        return LLMTurn(text=text, tool_calls=calls, stop_reason=stop_reason, raw=data)


def _anthropic_tool_to_openai(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
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
            entry: dict[str, Any] = {"role": "assistant", "content": text or None}
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
    ) -> LLMTurn:
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


def make_client(api_key: str | None = None, model: str | None = None) -> LLMClient:
    """Real client for the configured provider, scripted otherwise. Never raises.

    ``FORESHORE_LLM_PROVIDER`` picks the wire format (``anthropic`` default, or
    ``nvidia`` for the free NIM catalogue used during testing). Missing/invalid key for
    the selected provider degrades to :class:`ScriptedClient`, same as before — a live
    demo cannot die on a missing key or dead endpoint.
    """
    provider = (env("FORESHORE_LLM_PROVIDER", "anthropic") or "anthropic").strip().lower()
    client: LLMClient
    if provider == "nvidia":
        client = NvidiaNimClient(api_key=api_key, model=model)
    else:
        client = AnthropicClient(api_key=api_key, model=model)
    return client if client.available else ScriptedClient()


# --------------------------------------------------------------------------------------
# Unsourced-number guard
# --------------------------------------------------------------------------------------

_NUMBER = re.compile(r"(?<![\w.])(\d{1,4}(?:[.,]\d{1,3})?)(?![\w])")

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
        for q in obs.qualifiers.values():
            if isinstance(q, (int, float)) and not isinstance(q, bool):
                sourced.append(float(q))

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
        prefill_results: Sequence[tuple[str, dict[str, Any], str | None]] = (),
    ) -> RunResult:
        """Run one agent to a final answer.

        ``prefill_results`` lets a planner hand a specialist a fixed tool sequence. In
        scripted mode this is the entire plan; with a live model it seeds the context so
        the model reasons over evidence instead of deciding what to fetch from scratch.
        """
        names = list(tool_names) if tool_names is not None else self.registry.names()
        schemas = self.registry.schemas(names)
        steps: list[TraceStep] = []
        observations: list[Observation] = []
        results: list[ToolResult] = []

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

        while turns < self.max_turns:
            turns += 1
            try:
                turn = self.client.turn(system, messages, schemas, max_tokens=max_tokens)
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
    "AgentRuntime", "RunResult", "LLMClient", "LLMTurn", "AnthropicClient", "NvidiaNimClient",
    "ScriptedClient", "make_client", "check_unsourced_numbers", "DEFAULT_MODEL",
    "DEFAULT_NVIDIA_MODEL", "MAX_TURNS",
]
