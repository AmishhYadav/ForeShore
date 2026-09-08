"""Tests for ``foreshore.agents.specialists`` — the ten-specialist catalogue.

``tools/registry.py::SPECIALISTS`` names ten specialists (the problem statement's own
"ten cooperating AI agents"). Until this module added ``PlanningAgent`` and
``UserInteraction``, ``SPECIALIST_DEFS`` only defined eight of them, so
``GET /api/architecture`` — built straight from ``SPECIALIST_DEFS`` — advertised two
agents that did not exist. This file is the regression test for that gap: it can never
again pass while the two catalogues disagree.

Also covers the tool-ownership question raised by giving ``PlanningAgent`` the same
single tool as ``MarineDataDiscovery`` (``list_available_data``): ``specialist_for_tool``
must resolve every registered tool to exactly one specialist, deterministically, on every
call — never a coin flip driven by set iteration order.

Mirrors ``test_routes_reference.py``'s convention: a throwaway ``FastAPI`` app with only
the relevant router mounted, rather than importing ``foreshore.api.main`` (whose lifespan
starts the push-loop background thread on import).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from foreshore.agents import specialists
from foreshore.agents.specialists import SPECIALIST_DEFS, architecture, specialist_for_tool
from foreshore.api.routes_reference import router
from foreshore.tools.registry import SPECIALISTS, registry


def test_specialist_defs_matches_registry_specialists():
    """The catalogue can never again advertise an agent that does not exist — and never
    again quietly drop one either."""
    assert {s.name for s in SPECIALIST_DEFS} == set(SPECIALISTS)
    assert len(SPECIALIST_DEFS) == 10


def test_architecture_returns_ten_well_formed_entries():
    entries = architecture()
    assert len(entries) == 10
    for entry in entries:
        assert entry["name"], entry
        assert entry["role"].strip(), entry
        assert entry["ps_capability"].strip(), entry
        # tools may legitimately be empty (UserInteraction) but must always be present.
        assert isinstance(entry["tools"], list)


def test_every_specialist_tool_is_actually_registered():
    """Catches a typo'd tool name in any specialist's ``tools`` tuple."""
    for spec in SPECIALIST_DEFS:
        for tool_name in spec.tools:
            assert tool_name in registry, (
                f"{spec.name} claims tool {tool_name!r}, which is not registered"
            )


def test_specialist_for_tool_is_deterministic_for_every_registered_tool():
    """No dependence on set iteration order: calling this twice, and calling it for every
    registered tool, must always agree with itself."""
    for tool_name in registry.names():
        first = specialist_for_tool(tool_name)
        for _ in range(5):
            assert specialist_for_tool(tool_name) == first
    # A tool nobody claims resolves to None, not an exception.
    assert specialist_for_tool("not_a_real_tool") is None


def test_list_available_data_ownership_is_explicit_and_stable():
    """``list_available_data`` is deliberately shared by MarineDataDiscovery (its sole
    tool) and PlanningAgent (its sole tool, for horizon reasoning). Planning-step
    attribution stays with MarineDataDiscovery — unchanged from before PlanningAgent
    existed, and matching ``planner._step``'s own explicit fallback name — because
    MarineDataDiscovery is defined first in ``SPECIALIST_DEFS``. This pins that choice so
    a future reordering of the tuple cannot silently move it.
    """
    assert "list_available_data" in specialists.get("MarineDataDiscovery").tools
    assert "list_available_data" in specialists.get("PlanningAgent").tools
    assert specialist_for_tool("list_available_data") == "MarineDataDiscovery"


def test_user_interaction_has_no_tools():
    """A specialist with no tools is fine; it is the sole thing this specialist proves
    is real rather than a label in a tuple."""
    ui = specialists.get("UserInteraction")
    assert ui.tools == ()


def test_planning_agent_and_user_interaction_are_not_bare_labels():
    for name in ("PlanningAgent", "UserInteraction"):
        spec = specialists.get(name)
        assert spec.role.strip()
        assert spec.system.strip()
        assert spec.ps_capability.strip()
        # The prompt assembly must not blow up for a specialist with an empty tool tuple.
        # `prompt()` is COMMON_RULES + role + system and deliberately never interpolates
        # the specialist's own name — the name is a catalogue label, not something the
        # model needs told. So assert the assembly is well-formed, not that it self-names.
        prompt = spec.prompt()
        assert spec.role in prompt
        assert spec.system in prompt


def test_get_architecture_route_returns_ten():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    resp = client.get("/api/architecture")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["specialists"]) == 10
    names = {entry["name"] for entry in body["specialists"]}
    assert names == set(SPECIALISTS)
