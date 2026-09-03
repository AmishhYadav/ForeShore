"""Failure drill — the LLM connection dies mid-conversation, not before it starts.

``backend/tests/test_orchestrator.py``'s ``_Selective403Client`` already covers "a data
source 403s mid-pipeline". What nothing in the suite exercises yet is the sibling case
from PLAN.md's risk register and Phase 8 failure drills: the *model* call itself times
out or 5xxs after the conversation is already underway — a real, ``available`` client
(the ``ANTHROPIC_API_KEY`` path, not the no-key ``ScriptedClient`` fallback that already
has coverage elsewhere) whose ``.turn()`` raises partway through ``AgentRuntime.run()``'s
loop.

``runtime.py`` documents the intended behaviour in two places: the module docstring
("A live demo cannot be lost to an API outage") and a comment directly on the ``try`` in
``run()`` — "an LLM outage must degrade, not crash". This file exists to prove that is
actually true, not merely asserted in a comment: the call must return a ``RunResult``
rather than propagate, the failure must be visible on that result (not swallowed into a
silent empty success), and any tool evidence gathered on turns before the failure must
survive it — a mid-run outage must not throw away work already done.
"""

from __future__ import annotations

from typing import Any

from foreshore.agents.runtime import AgentRuntime, LLMTurn, ScriptedClient
from foreshore.models import ToolResult
from foreshore.tools.registry import ToolRegistry


class _DiesAfterFirstTurnClient:
    """Stands in for a configured, reachable Anthropic client (``available = True``,
    and deliberately *not* a :class:`ScriptedClient` instance) whose underlying
    connection is fine for one turn and then drops — the shape of a network timeout or
    a mid-session 5xx, not a missing API key. ``AgentRuntime.run`` only takes the
    scripted-mode early return when the client is unavailable or actually scripted
    (``runtime.py`` line ~343), so this class exists purely to force the loop through
    its real ``while`` body and into the ``try/except`` around ``self.client.turn(...)``.

    Turn 1 asks for one tool call (so the run has gathered real evidence before things
    go wrong); turn 2 raises, simulating the outage arriving mid-conversation.
    """

    available = True
    name = "fake-anthropic:mid-run-outage"

    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name
        self.calls = 0

    def turn(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> LLMTurn:
        self.calls += 1
        if self.calls == 1:
            return LLMTurn(
                text="",
                tool_calls=[{"id": "call_1", "name": self._tool_name, "input": {}}],
                stop_reason="tool_use",
            )
        # Simulated network timeout / dropped connection after the socket was already
        # open — not an ANTHROPIC_API_KEY problem, which is why this is a generic
        # exception rather than anything anthropic-specific.
        raise TimeoutError("simulated network timeout mid-conversation")


def _build_single_tool_registry(tool_name: str, observation) -> ToolRegistry:
    """A minimal, isolated registry (not the process-wide default) carrying exactly one
    deterministic test tool, so this drill does not depend on real source adapters or
    on ``FORESHORE_MODE=fixture`` snapshot data that other work in this repo may or may
    not have populated yet."""
    reg = ToolRegistry()

    @reg.tool(
        name=tool_name,
        number=1,
        description="Deterministic stand-in tool for the mid-run LLM outage drill.",
        schema={"type": "object", "properties": {}, "required": []},
        specialists=["OceanAnalytics"],
    )
    def _handler(**_ignored: Any) -> ToolResult:
        return ToolResult(
            tool=tool_name,
            ok=True,
            observations=[observation],
            summary="Fake wave reading gathered before the outage.",
        )

    return reg


def test_llm_turn_exception_mid_run_degrades_instead_of_crashing(tmp_path, make_observation):
    """The behaviour this whole file exists to pin down.

    Sequence: turn 1 succeeds and calls a tool (evidence gathered); turn 2 raises. The
    run must still return normally, must surface the failure on the result rather than
    hide it, and must keep the evidence the first turn already collected.
    """
    from foreshore.store.traces import TraceStore

    tool_name = "fake_wave_reading"
    obs = make_observation(variable="significant_wave_height", value=1.4)
    registry = _build_single_tool_registry(tool_name, obs)
    client = _DiesAfterFirstTurnClient(tool_name)

    runtime = AgentRuntime(
        registry=registry,
        traces=TraceStore(path=tmp_path / "traces.jsonl"),
        client=client,
        query_id="test-llm-outage",
    )

    # -- sanity: this test is actually exercising the live-client loop, not the
    #    already-covered no-key ScriptedClient fallback ------------------------------
    assert client.available is True
    assert not isinstance(client, ScriptedClient)

    # -- 1. the call does not raise -----------------------------------------------------
    result = runtime.run(
        agent="OceanAnalytics",
        system="You are a test agent for the outage drill.",
        user_message="What are the wave conditions?",
        tool_names=[tool_name],
    )

    # -- 2. the failure is visibly recorded, not silently swallowed ---------------------
    assert result.error is not None
    assert "TimeoutError" in result.error
    assert result.ok is False
    assert result.stopped == "llm_error"
    # the second (failing) turn was attempted — the loop did not stop after the first
    assert client.calls == 2

    # a trace step records the failure too, so the outage shows up in the reasoning
    # trace an operator or judge would inspect, not just in the return value.
    error_steps = [s for s in result.steps if s.kind == "error"]
    assert len(error_steps) == 1
    assert error_steps[0].ok is False
    assert error_steps[0].error == result.error

    # -- 3. evidence gathered before the failure survives it -----------------------------
    assert len(result.tool_results) == 1
    assert result.tool_results[0].tool == tool_name
    assert result.tool_results[0].ok is True
    assert len(result.observations) == 1
    assert result.observations[0].variable == "significant_wave_height"
    assert result.observations[0].value == 1.4


def test_llm_turn_exception_on_the_very_first_call_still_degrades(tmp_path):
    """The simpler variant: the outage hits before any tool has ever been called, i.e.
    the connection never really came up. There is no evidence to preserve here, but the
    result must still be a well-formed, non-raising ``RunResult`` with the failure
    visible — this is the "outage on connect" cousin of the mid-run drill above, and
    catches an implementation that only handles the exception on turns after the first.
    """
    from foreshore.store.traces import TraceStore
    from foreshore.tools.registry import registry as default_registry

    class _DiesImmediately:
        available = True
        name = "fake-anthropic:dies-immediately"

        def turn(self, system, messages, tools, *, max_tokens=2048, temperature=0.0):
            raise RuntimeError("simulated API 5xx on the very first turn")

    runtime = AgentRuntime(
        registry=default_registry,
        traces=TraceStore(path=tmp_path / "traces.jsonl"),
        client=_DiesImmediately(),
        query_id="test-llm-outage-first-call",
    )

    result = runtime.run(
        agent="OceanAnalytics",
        system="You are a test agent for the outage drill.",
        user_message="What are the wave conditions?",
        tool_names=[],
    )

    assert result.error is not None
    assert "RuntimeError" in result.error
    assert result.ok is False
    assert result.stopped == "llm_error"
    assert result.tool_results == []
    assert result.observations == []
    assert result.turns == 1
