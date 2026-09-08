"""Tests for the conversational front door — ``agents/conversation.py``'s handlers and
the short-circuit seam in ``agents/orchestrator.py::answer``.

``conversation.classify_utterance`` itself is a fixed contract (this module does not
touch it); what is under test here is everything built on top of it:

1. Every kind in the table below routes to the door the module docstring says it
   should.
2. **The anti-regression test.** All eight PS canonical queries must still classify
   ``OPERATIONAL`` — a front door that accidentally swallows a real marine question
   into CONCEPT or CAPABILITY is worse than having no front door at all.
3. ``distress_reply`` resolves a named handoff from the local vector store alone, and
   keeps working with the INCOIS adapter broken — it must never depend on the network.
4. The provenance rule ``config/glossary.yaml`` documents at its own top: an entry with
   ``sourced_from: null`` is digit-free, in both ``en`` and ``ta``.
5. A short-circuited ``orchestrator.answer()`` call produces ``verdict is None`` and a
   ``QueryOutcome.to_dict()`` that still carries every key a normal answer carries.
"""

from __future__ import annotations

import re

import pytest

from foreshore.agents import conversation, orchestrator
from foreshore.agents.conversation import (
    SHORT_CIRCUIT_KINDS,
    capability_reply,
    classify_utterance,
    concept_reply,
    distress_reply,
    out_of_scope_reply,
    smalltalk_reply,
)
from foreshore.agents.orchestrator import Query
from foreshore.config import load_glossary, load_region
from foreshore.models import Handoff

_GLOSSARY = load_glossary()
_ALIASES = _GLOSSARY.aliases()
_REGION = load_region()
_PORT = _REGION.anchor_ports[0]

#: Internal enum codes / identifiers that must never leak into user-facing prose.
_FORBIDDEN_TOKENS = ("MPA", "BREACH", "DO_NOT_ADVISE", "GO_WITH_CAUTION", "Traceback")


def _classify(text: str) -> str:
    return classify_utterance(text, glossary_terms=_ALIASES)


# --------------------------------------------------------------------------------------
# 1. Routing table.
# --------------------------------------------------------------------------------------

ROUTING_TABLE: list[tuple[str, str]] = [
    ("hello", "SMALLTALK"),
    ("good morning", "SMALLTALK"),
    ("thanks", "SMALLTALK"),
    ("who made you?", "CAPABILITY"),
    ("what can you do?", "CAPABILITY"),
    ("help", "CAPABILITY"),
    ("tell me a joke", "OUT_OF_SCOPE"),
    ("what is the price of prawns", "OUT_OF_SCOPE"),
    ("what is chlorophyll?", "CONCEPT"),
    ("explain PFZ to me", "CONCEPT"),
    ("my engine failed", "DISTRESS"),
    ("we are taking on water", "DISTRESS"),
    ("SOS", "DISTRESS"),
    ("how deep is the water here", "OPERATIONAL"),
    ("what is the wave height today", "OPERATIONAL"),
    ("hello, is it safe to go out?", "OPERATIONAL"),
    ("engine failed, is it safe to go back?", "DISTRESS"),
]


@pytest.mark.parametrize("text,expected", ROUTING_TABLE)
def test_utterance_routes_to_expected_door(text: str, expected: str) -> None:
    assert _classify(text) == expected, f"{text!r} classified as {_classify(text)!r}"


# --------------------------------------------------------------------------------------
# 2. Anti-regression: every canonical PS query stays OPERATIONAL.
# --------------------------------------------------------------------------------------

CANONICAL_PS_QUERIES: tuple[str, ...] = (
    "Nearest Potential Fishing Zone today",
    "Is it safe to venture out tomorrow morning?",
    "Tide, weather, sea conditions near my location",
    "Lightning or cyclone alerts in my area",
    "Regions with high chlorophyll and favourable SST",
    "Safest route given weather and sea state",
    "Why has fish productivity declined in a region?",
    "Which zones to avoid (hazard or geofencing)",
)


@pytest.mark.parametrize("text", CANONICAL_PS_QUERIES)
def test_canonical_ps_query_stays_operational(text: str) -> None:
    """The front door must never intercept a real marine question. This is the single
    most important test in this file — a false CONCEPT/CAPABILITY/OUT_OF_SCOPE hit here
    means a judge's own demo question gets a glossary blurb instead of a verdict."""
    assert _classify(text) == "OPERATIONAL"


def test_canonical_queries_still_reach_the_full_pipeline_via_orchestrator() -> None:
    """End-to-end: a canonical query must not be short-circuited by
    ``orchestrator.answer`` — it should plan real steps and reach a verdict, not the
    conversational front door's ``UserInteraction`` trace step."""
    outcome = orchestrator.answer(
        Query(text="Is it safe to venture out tomorrow morning?", lat=_PORT.lat, lon=_PORT.lon),
    )
    assert outcome.plan.steps, "a canonical query must still produce a real plan"
    assert outcome.answer.payloads.get("utterance_kind") is None


# --------------------------------------------------------------------------------------
# 3. distress_reply — the most important handler. Local-only, network-independent.
# --------------------------------------------------------------------------------------


def test_distress_reply_returns_a_named_handoff() -> None:
    reply = distress_reply(_PORT.lat, _PORT.lon, region=_REGION)
    assert reply.handoff is not None
    assert isinstance(reply.handoff, Handoff)
    assert reply.handoff.authority_name
    assert reply.handoff.reason
    # Coast Guard / VHF 16 always named, whichever branch resolved.
    assert "1554" in reply.text
    assert "16" in reply.text


def test_distress_reply_resolves_from_local_store_never_from_incois_wfs(monkeypatch) -> None:
    """Monkeypatch the INCOIS adapter to raise on construction and on every method —
    distress_reply must never touch it, so this must not affect the outcome at all."""
    from foreshore.sources import incois_wfs as incois_wfs_module

    def _boom(*args, **kwargs):
        raise RuntimeError("network is down for this test")

    monkeypatch.setattr(incois_wfs_module.IncoisWFS, "__init__", _boom)
    monkeypatch.setattr(incois_wfs_module.IncoisWFS, "nearest_landing_centres", _boom)
    monkeypatch.setattr(incois_wfs_module.IncoisWFS, "landing_centres", _boom)

    reply = distress_reply(_PORT.lat, _PORT.lon, region=_REGION)
    assert reply.handoff is not None
    assert reply.handoff.authority_name
    assert reply.text


def test_distress_reply_falls_back_to_regional_coast_guard_far_from_any_centre() -> None:
    """A position nowhere near any configured landing centre still gets a named
    handoff — the regional Coast Guard line, never silence."""
    reply = distress_reply(0.0, 0.0, region=_REGION)
    assert reply.handoff is not None
    assert reply.handoff.authority_type == "coast_guard"
    assert reply.handoff.contact_verified is True


def test_distress_wins_over_operational_cues_in_the_same_utterance() -> None:
    assert _classify("engine failed, is it safe to go back?") == "DISTRESS"


# --------------------------------------------------------------------------------------
# 4. Glossary provenance rule — sourced_from: null entries must be digit-free.
# --------------------------------------------------------------------------------------

_DIGIT = re.compile(r"\d")


def test_glossary_has_expected_concepts() -> None:
    expected_keys = {
        "pfz", "chlorophyll", "sea_surface_temperature", "douglas_scale",
        "significant_wave_height", "swell_period", "kallakkadal", "imbl", "geofence",
        "marine_protected_area", "upwelling", "verdict_levels", "advisory_ceiling",
    }
    assert expected_keys <= {t.key for t in _GLOSSARY.terms}


def test_null_sourced_glossary_entries_are_digit_free() -> None:
    offenders = []
    for term in _GLOSSARY.terms:
        if term.sourced_from is not None:
            continue
        if _DIGIT.search(term.en):
            offenders.append((term.key, "en"))
        if term.ta and _DIGIT.search(term.ta):
            offenders.append((term.key, "ta"))
    assert not offenders, f"digit-free glossary entries contain a number: {offenders}"


def test_sourced_glossary_entries_declare_a_known_kind() -> None:
    known = {"douglas_table", "vessel_limits", "region_config"}
    for term in _GLOSSARY.terms:
        if term.sourced_from is not None:
            assert term.sourced_from in known, term.key


def test_concept_reply_emits_observations_for_every_sourced_number() -> None:
    """douglas_scale and kallakkadal both name numbers, and both must come back with
    matching Observation records — the handler-side half of the provenance rule."""
    douglas = concept_reply("what is the douglas scale", glossary=_GLOSSARY, region=_REGION)
    assert douglas.observations, "douglas_scale prose has numbers but no observations"
    for obs in douglas.observations:
        assert obs.provenance is not None

    kallakkadal = concept_reply("what is kallakkadal", glossary=_GLOSSARY, region=_REGION)
    assert kallakkadal.observations
    assert any(o.variable == "long_period_swell_threshold_s" for o in kallakkadal.observations)

    # A definition with sourced_from: null carries no observations at all.
    pfz = concept_reply("explain PFZ to me", glossary=_GLOSSARY, region=_REGION)
    assert pfz.observations == ()


def test_concept_reply_text_matches_classify_utterance_routing() -> None:
    for text, expected in ROUTING_TABLE:
        if expected != "CONCEPT":
            continue
        reply = concept_reply(text, glossary=_GLOSSARY, region=_REGION)
        assert reply.text, text


# --------------------------------------------------------------------------------------
# 5. Short-circuited orchestrator.answer(): verdict is None, envelope is intact.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["hello", "what can you do?", "what is chlorophyll?", "tell me a joke", "my engine failed"],
)
def test_short_circuited_answer_has_no_verdict_and_a_valid_envelope(text: str) -> None:
    outcome = orchestrator.answer(Query(text=text, lat=_PORT.lat, lon=_PORT.lon))
    assert outcome.verdict is None
    assert outcome.answer.verdict is None
    assert _classify(text) in SHORT_CIRCUIT_KINDS

    d = outcome.to_dict()
    # Every key a normal answer carries must still be present.
    for key in (
        "query_id", "language", "text", "verdict", "evidence", "trace", "route",
        "payloads", "unsourced_numbers", "plan", "duration_ms", "missing",
        "specialists_used", "architecture", "scenario", "run_mode",
    ):
        assert key in d, f"missing key {key!r} for {text!r}"
    assert d["run_mode"] in ("live", "fixture")
    assert d["payloads"]["utterance_kind"] == _classify(text)
    assert d["text"]
    assert d["plan"]["steps"] == []


def test_short_circuit_records_one_trace_step_under_user_interaction() -> None:
    outcome = orchestrator.answer(Query(text="hello", lat=_PORT.lat, lon=_PORT.lon))
    assert len(outcome.trace) == 1
    step = outcome.trace[0]
    assert step.agent == "UserInteraction"
    assert step.kind == "plan"


def test_short_circuited_text_never_leaks_internal_tokens() -> None:
    for text, _ in ROUTING_TABLE:
        if _classify(text) not in SHORT_CIRCUIT_KINDS:
            continue
        outcome = orchestrator.answer(Query(text=text, lat=_PORT.lat, lon=_PORT.lon))
        for token in _FORBIDDEN_TOKENS:
            assert token not in outcome.answer.text, (text, token)


# --------------------------------------------------------------------------------------
# Direct handler smoke tests — capability / smalltalk / out-of-scope.
# --------------------------------------------------------------------------------------


def test_capability_reply_names_ten_specialists_from_the_live_registry() -> None:
    from foreshore.agents.specialists import SPECIALIST_DEFS

    reply = capability_reply(region=_REGION)
    for spec in SPECIALIST_DEFS:
        assert spec.name in reply.text
    assert reply.observations == ()


def test_smalltalk_and_out_of_scope_are_non_empty_and_carry_no_verdict_language() -> None:
    smalltalk = smalltalk_reply(region=_REGION)
    scope = out_of_scope_reply(region=_REGION)
    assert smalltalk.text
    assert scope.text
    for token in _FORBIDDEN_TOKENS:
        assert token not in smalltalk.text
        assert token not in scope.text
