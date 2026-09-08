"""Tests for tool 19, ``get_decision_envelope`` (backend/foreshore/tools/envelope_tools.py),
and the planner integration that routes operational-planning questions to it.

``verdict/envelope.py`` (a fixed contract for this task, covered directly by
``test_envelope.py``) does the actual decision logic -- which constraint binds, and
where the three derived times fall. This file only exercises the tool wrapper: that it
fetches the whole horizon with exactly one ``IncoisThredds.series(...)`` call (the
efficiency contract the tool exists to hold -- CLAUDE.md's own latency section names
cold INCOIS OSF NetCDF grids as the single expensive fetch in this system), that every
number in its ``summary`` traces to a returned ``Observation`` (invariant 3, checked the
same way ``test_productive_waters.py`` checks it), that a missing bulletin degrades to
``partial=True`` rather than crashing, and that the new planner cues route the right
questions to it without moving any of the eight PS-bullet sample queries
(``PROJECT_CONTEXT.md``, "The eight sample queries") onto a different plan.

``IncoisThredds.series`` is monkeypatched at the class boundary -- the same approach
``test_productive_waters.py`` and ``test_productivity.py`` use -- with small, hand-built
``Observation``/``Provenance`` records, so this suite opens no socket even though real
frozen fixtures now exist under ``data/fixtures/`` (which would otherwise make the
horizon-fetch path exercise real NetCDF/catalog parsing -- not what these tests are
about, and not deterministic enough for exact verdict-level assertions).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from foreshore.agents.planner import plan
from foreshore.agents.runtime import check_unsourced_numbers
from foreshore.models import ToolResult, utcnow
from foreshore.sources.incois_thredds import IncoisThredds
from foreshore.tools import registry
from foreshore.tools.envelope_tools import get_decision_envelope
from foreshore.tools.registry import ToolRegistry
from foreshore.tools.verdict_tools import clear_evidence, record_evidence

# --------------------------------------------------------------------------------------
# Fakes: one INCOIS OSF wave series (4 steps, 3 h apart) and one bulletin, hand-built so
# every verdict level in the horizon is exactly controllable.
# --------------------------------------------------------------------------------------

#: Hs per step (m): GO, GO_WITH_CAUTION, DO_NOT_ADVISE, GO again -- against
#: small_motorised's config/vessels.yaml limits (go 1.25 m, caution 2.5 m), so the
#: window opens, closes, and reopens exactly once across the horizon.
_HS_VALUES = (0.6, 1.6, 3.0, 0.5)
_PERIOD_S = 8.0


def _patch_series(monkeypatch, make_observation, make_provenance, *, now, count_holder=None):
    """Replace ``IncoisThredds.series`` with a fake returning ``_HS_VALUES`` as
    significant-wave-height + wave-period Observations, 3 h apart, starting at ``now``.
    ``count_holder``, if given, is incremented on every call -- the efficiency proof."""

    def fake_series(self, product, lat, lon, *, variables=None, hours=48):
        if count_holder is not None:
            count_holder["n"] += 1
        assert product == "wave"
        out = []
        for i, hs in enumerate(_HS_VALUES):
            when = now + timedelta(hours=3 * i)
            prov = make_provenance(
                source_id="incois_osf_wave", source_name="fake INCOIS OSF wave",
                authority="INCOIS", _now=when,
                valid_from=when - timedelta(hours=1), valid_to=when + timedelta(hours=2),
            )
            out.append(make_observation(
                variable="significant_wave_height", value=hs, unit="m",
                lat=lat, lon=lon, valid_time=when, provenance=prov,
            ))
            out.append(make_observation(
                variable="wave_period", value=_PERIOD_S, unit="s",
                lat=lat, lon=lon, valid_time=when, provenance=prov,
            ))
        return out

    monkeypatch.setattr(IncoisThredds, "series", fake_series)


def _seed_bulletin(make_observation, make_provenance, region, query_id, *, now, sea_condition="SLIGHT"):
    """Record a bulletin whose Douglas band cap (SLIGHT -> GO for small_motorised) never
    restricts below whatever the raw wave thresholds compute, and whose validity window
    comfortably covers the whole 4-step horizon -- so every level in ``_HS_VALUES``
    reaches the envelope unaltered by the ceiling."""
    lat0, lon0 = region.centre
    prov = make_provenance(
        source_id="imd_coastal_bulletin", source_name="fake IMD bulletin", authority="IMD",
        _now=now, issued_at=now - timedelta(hours=1),
        valid_from=now - timedelta(hours=1), valid_to=now + timedelta(hours=50),
    )
    obs = make_observation(
        variable="sea_condition", value=sea_condition, unit="descriptor",
        lat=lat0, lon=lon0, valid_time=now, provenance=prov,
        qualifiers={"coast_block": "TEST_BLOCK"},
    )
    record_evidence(query_id, [obs])


# --------------------------------------------------------------------------------------
# 1. Well-formed ToolResult, non-empty payload["steps"].
# --------------------------------------------------------------------------------------


def test_well_formed_result_with_non_empty_steps(monkeypatch, make_observation, make_provenance, region):
    now = utcnow()
    _patch_series(monkeypatch, make_observation, make_provenance, now=now)
    qid = "envelope-test-well-formed"
    _seed_bulletin(make_observation, make_provenance, region, qid, now=now)
    try:
        result = get_decision_envelope(
            lat=region.centre[0], lon=region.centre[1],
            horizon_hours=9, return_duration_hours=1, evidence_query_id=qid,
        )
    finally:
        clear_evidence(qid)

    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.partial is False
    assert result.missing == []
    assert result.payload["steps"], "expected a non-empty list of envelope steps"
    assert len(result.payload["steps"]) == len(_HS_VALUES)
    levels = [s["level"] for s in result.payload["steps"]]
    assert levels == ["GO", "GO_WITH_CAUTION", "DO_NOT_ADVISE", "GO"]

    # The three derived times, all exercised by this fixture.
    assert result.payload["latest_safe_departure"] is not None
    assert result.payload["next_go_window"] is not None
    assert result.payload["turn_back_by"] is not None

    assert result.observations, "expected sourced Observations, not just a payload"
    for obs in result.observations:
        assert obs.provenance is not None


# --------------------------------------------------------------------------------------
# 2. Exactly one series() call for a full horizon -- the efficiency contract.
# --------------------------------------------------------------------------------------


def test_series_is_called_exactly_once(monkeypatch, make_observation, make_provenance, region):
    now = utcnow()
    calls = {"n": 0}
    _patch_series(monkeypatch, make_observation, make_provenance, now=now, count_holder=calls)
    qid = "envelope-test-one-call"
    _seed_bulletin(make_observation, make_provenance, region, qid, now=now)
    try:
        result = get_decision_envelope(
            lat=region.centre[0], lon=region.centre[1],
            horizon_hours=168, evidence_query_id=qid,
        )
    finally:
        clear_evidence(qid)

    assert result.ok is True
    assert calls["n"] == 1, f"expected exactly one series() call, got {calls['n']}"


# --------------------------------------------------------------------------------------
# 3. Every number in `summary` traces to an Observation (invariant 3).
# --------------------------------------------------------------------------------------


def test_summary_numbers_all_traceable_to_observations(monkeypatch, make_observation, make_provenance, region):
    now = utcnow()
    _patch_series(monkeypatch, make_observation, make_provenance, now=now)
    qid = "envelope-test-unsourced"
    _seed_bulletin(make_observation, make_provenance, region, qid, now=now)
    try:
        result = get_decision_envelope(
            lat=region.centre[0], lon=region.centre[1],
            horizon_hours=9, return_duration_hours=1, evidence_query_id=qid,
        )
    finally:
        clear_evidence(qid)

    assert result.summary, "expected non-empty prose"
    bad = check_unsourced_numbers(result.summary, result.observations)
    assert bad == [], f"summary contains numbers not traceable to any Observation: {bad}"


# --------------------------------------------------------------------------------------
# 4. `summary` is clean user-facing prose: no enum code, no underscore identifier, no
#    tool name.
# --------------------------------------------------------------------------------------


def test_summary_has_no_enum_code_underscore_or_tool_name(monkeypatch, make_observation, make_provenance, region):
    now = utcnow()
    _patch_series(monkeypatch, make_observation, make_provenance, now=now)
    qid = "envelope-test-clean-prose"
    _seed_bulletin(make_observation, make_provenance, region, qid, now=now)
    try:
        result = get_decision_envelope(
            lat=region.centre[0], lon=region.centre[1],
            horizon_hours=9, return_duration_hours=1, evidence_query_id=qid,
        )
    finally:
        clear_evidence(qid)

    summary = result.summary
    assert "_" not in summary, "summary must not contain a source id or internal identifier"
    for tool_name in registry.names():
        assert tool_name not in summary, f"internal tool name {tool_name!r} leaked into summary"
    assert "Error" not in summary and "Exception" not in summary
    for code in ("DO_NOT_ADVISE", "GO_WITH_CAUTION", "MPA", "BREACH"):
        assert code not in summary


# --------------------------------------------------------------------------------------
# 5. Missing bulletin -> partial=True, missing names it, never a crash.
# --------------------------------------------------------------------------------------


def test_missing_bulletin_is_partial_not_a_crash(monkeypatch, make_observation, make_provenance, region):
    now = utcnow()
    _patch_series(monkeypatch, make_observation, make_provenance, now=now)

    def _fake_call(name, args=None):
        # get_decision_envelope's only registry.call() is the get_governing_advisory
        # fallback when the evidence bus is empty -- force it to abstain deterministically
        # rather than depending on whatever data/fixtures/imd_coastal_bulletin happens to
        # hold right now.
        assert name == "get_governing_advisory"
        return ToolResult(
            tool=name, ok=True, partial=True, observations=[],
            missing=["imd_coastal_bulletin"], summary="no bulletin available",
        )

    monkeypatch.setattr(registry, "call", _fake_call)

    result = get_decision_envelope(lat=region.centre[0], lon=region.centre[1], horizon_hours=9)

    assert result.ok is True, "a missing bulletin must degrade, never raise"
    assert result.partial is True
    assert "imd_coastal_bulletin" in result.missing
    # Not a silent gap: every step still comes back, closed, via the ceiling's own
    # missing_required_input rule -- DECISIONS.md-style correct behaviour, not a bug to
    # route around.
    assert result.payload["steps"]
    for step in result.payload["steps"]:
        assert step["level"] == "DO_NOT_ADVISE"
    assert "bulletin" in result.summary.lower()


# --------------------------------------------------------------------------------------
# 6. Planner integration -- operational-planning cues route to the tool.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "When can I go out today?",
        "How long have I got before the weather turns?",
        "When must I turn back?",
        "What's my weather window this weekend?",
        "What's the best time to head out tomorrow?",
    ],
)
def test_operational_planning_questions_route_to_the_envelope_tool(text: str) -> None:
    p = plan(text, query_id="envelope-planner-test")
    assert "get_decision_envelope" in p.tools()
    # These are somebody deciding whether and when to put to sea, stretched over a
    # window rather than asked about one instant -- the same shape of question as
    # "should I go", so they lead with the verdict too.
    assert p.answer_kind == "ADVISORY"


def test_operational_planning_cue_does_not_fire_on_an_unrelated_information_question() -> None:
    p = plan("What ships are closest to the IMBL right now?", query_id="envelope-planner-test-2")
    assert "get_decision_envelope" not in p.tools()


# --------------------------------------------------------------------------------------
# 7. Anti-regression -- the eight PS-bullet sample queries still plan exactly the tools
#    they planned before this change (PROJECT_CONTEXT.md, "The eight sample queries").
# --------------------------------------------------------------------------------------

_EIGHT_CANONICAL_QUERIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Nearest Potential Fishing Zone today", (
        "get_governing_advisory", "get_sea_state", "get_weather", "find_nearest_pfz",
        "derive_pfz_zones", "check_geofences", "nearest_harbour", "evaluate_verdict",
    )),
    ("Is it safe to venture out tomorrow morning?", (
        "get_governing_advisory", "get_sea_state", "get_weather",
        "check_geofences", "nearest_harbour", "evaluate_verdict",
    )),
    ("Tide, weather, sea conditions near my location", (
        "get_governing_advisory", "get_sea_state", "get_weather", "get_tide",
        "get_currents", "check_geofences", "nearest_harbour", "evaluate_verdict",
    )),
    ("Lightning or cyclone alerts in my area", (
        "get_governing_advisory", "get_sea_state", "get_weather", "get_hazard_alerts",
        "get_lightning_nowcast", "check_geofences", "nearest_harbour", "evaluate_verdict",
    )),
    ("Regions with high chlorophyll and favourable SST", (
        "get_governing_advisory", "get_sea_state", "get_weather", "find_productive_waters",
        "derive_pfz_zones", "check_geofences", "nearest_harbour", "evaluate_verdict",
    )),
    ("Safest route given weather and sea state", (
        "get_governing_advisory", "get_sea_state", "get_weather", "plan_route",
        "get_exclusion_zones", "check_geofences", "nearest_harbour", "evaluate_verdict",
    )),
    ("Why has fish productivity declined in a region?", (
        "get_governing_advisory", "get_sea_state", "get_weather", "get_productivity_history",
        "find_nearest_pfz", "derive_pfz_zones", "check_geofences", "nearest_harbour",
        "evaluate_verdict",
    )),
    ("Which zones to avoid (hazard or geofencing)", (
        "get_governing_advisory", "get_sea_state", "get_weather", "get_exclusion_zones",
        "get_hazard_alerts", "check_geofences", "get_lightning_nowcast", "nearest_harbour",
        "evaluate_verdict",
    )),
)


@pytest.mark.parametrize("text,expected_tools", _EIGHT_CANONICAL_QUERIES, ids=[q for q, _ in _EIGHT_CANONICAL_QUERIES])
def test_eight_canonical_ps_queries_do_not_regress(text: str, expected_tools: tuple[str, ...]) -> None:
    p = plan(text, query_id="envelope-regression-test")
    assert p.tools() == list(expected_tools)
    assert "get_decision_envelope" not in p.tools()
