"""The question decides the shape of the answer.

Before this, every query ran the same advisory pipeline and every answer opened with the
verdict — so "which vessels are closest to the IMBL right now?" came back as "Do not go.",
a refusal-shaped reply to a question that was never about going anywhere. The verdict, the
advisory ceiling and the evidence audit are unchanged by all of this; what these tests pin
down is that the verdict stops pretending to be the answer to a question that did not ask
for it, and that it is never dropped, softened or hidden when it is.
"""

from __future__ import annotations

import pytest

from foreshore.agents.planner import classify_answer_kind, fleet_geofence_class, plan
from foreshore.agents.synthesis import (
    answers_the_question,
    as_sentences,
    enforce_answer_contract,
    handoff_present,
    template_answer,
)
from foreshore.models import Handoff, Provenance, Verdict, utcnow


# ---------------------------------------------------------------------------------------
# classify_answer_kind
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Is it safe to go out this morning?",
        "Should I go out tonight?",
        "Can I go to the fishing grounds?",
        "What is the safest way to Nagapattinam?",
        "what if I leave at 04:00 instead of 06:00",
        "kadal poga pogalama",
        # Rule 3, the safety default: nothing classifiable at all still gets the
        # advisory framing rather than being treated as a question about the world.
        "asdf qwerty",
        "",
    ],
)
def test_decision_questions_are_advisory(text: str) -> None:
    assert classify_answer_kind(text) == "ADVISORY"


@pytest.mark.parametrize(
    "text",
    [
        "What ships are closest to the IMBL right now?",
        "Which vessels are closest to the IMBL right now?",
        "Why has fish productivity declined here?",
        "Where can I fish tomorrow?",
        "How many boats are inside the marine national park?",
        "What is the tide tonight?",
        "How far is the boundary from here?",
        "Show me the cyclone warnings",
    ],
)
def test_information_questions_are_informational(text: str) -> None:
    assert classify_answer_kind(text) == "INFORMATIONAL"


def test_a_decision_cue_beats_an_interrogative() -> None:
    """"What is the wave height, and should I go?" is a decision. The reader needs the
    decision first; the fact is the reason for it."""
    assert classify_answer_kind("What is the wave height, and should I go?") == "ADVISORY"


def test_fleet_class_is_derived_from_class_vocabulary_not_place_names() -> None:
    assert fleet_geofence_class("which vessels are near the IMBL") == "IMBL"
    assert fleet_geofence_class("boats inside the marine national park") == "MPA"
    assert fleet_geofence_class("vessels near the coral reef") == "ECO_SENSITIVE"
    assert fleet_geofence_class("boats in the cyclone exclusion zone") == "HAZARD_EXCLUSION"
    assert fleet_geofence_class("where are the boats") is None


# ---------------------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------------------


def test_plan_carries_answer_kind_and_never_drops_the_safety_spine() -> None:
    """Presentation changes; the spine does not. Both kinds still fetch the governing
    bulletin, the sea state and the weather, and both still end in a verdict."""
    for text in ("Should I go out?", "Which vessels are closest to the IMBL?"):
        p = plan(text, query_id="t")
        tools = p.tools()
        for required in (
            "get_governing_advisory", "get_sea_state", "get_weather",
            "check_geofences", "nearest_harbour", "evaluate_verdict",
        ):
            assert required in tools, f"{required} missing from plan for {text!r}"
        assert p.to_dict()["answer_kind"] == p.answer_kind


def test_a_fleet_question_plans_the_fleet_tool_ahead_of_the_safety_geofence_check() -> None:
    """The boundary word in "which vessels are closest to the IMBL" qualifies the fleet
    question; it is not a second question about the asker's own position. Tool 17 answers
    first and `check_geofences` stays as the mandatory safety add."""
    tools = plan("Which vessels are closest to the IMBL right now?", query_id="t").tools()
    assert "find_vessels_near_boundary" in tools
    assert "check_geofences" in tools
    assert tools.index("find_vessels_near_boundary") < tools.index("check_geofences")


# ---------------------------------------------------------------------------------------
# template_answer
# ---------------------------------------------------------------------------------------


def _verdict(level: str = "DO_NOT_ADVISE", **kw) -> Verdict:
    prov = Provenance(
        source_id="test", source_name="Test", authority="official",
        url="https://example.invalid", acquired_at=utcnow(),
    )
    handoff = Handoff(
        reason="test", authority_name="Rameswaram Fishing Harbour",
        authority_type="landing_centre", contact="0123 456", contact_verified=False,
        distance_nm=0.5, provenance=prov,
    )
    return Verdict(
        level=level,  # type: ignore[arg-type]
        reasons=kw.pop("reasons", ["The IMD bulletin expired 45 h ago."]),
        handoff=handoff if level == "DO_NOT_ADVISE" else None,
        **kw,
    )


FINDING = "Three vessels are within 2 nm of the 1974 line"


def test_advisory_answers_open_with_the_verdict() -> None:
    text = template_answer(_verdict(), "en", extras=[FINDING], answer_kind="ADVISORY")
    assert text.startswith("Do not go.")


def test_informational_answers_do_not_open_with_a_bare_verdict() -> None:
    """The regression this whole change exists for."""
    text = template_answer(_verdict(), "en", extras=[FINDING], answer_kind="INFORMATIONAL")
    assert not text.startswith("Do not go.")
    assert "Safety note" in text
    # Still unmissable, still complete: nothing about the abstention is dropped.
    assert "Do not go." in text
    assert "Rameswaram Fishing Harbour" in text
    assert FINDING in text


def test_informational_and_not_go_puts_the_safety_lead_before_the_answer() -> None:
    text = template_answer(_verdict(), "en", extras=[FINDING], answer_kind="INFORMATIONAL")
    assert text.index("Do not go.") < text.index(FINDING)


def test_informational_and_go_puts_the_answer_first() -> None:
    text = template_answer(
        _verdict("GO"), "en", extras=[FINDING], answer_kind="INFORMATIONAL"
    )
    assert text.startswith(FINDING)
    assert text.index(FINDING) < text.index("Safe to go")


def test_no_verdict_codes_reach_the_prose() -> None:
    """`downgraded_from`/`level` are storage values. They used to be printed raw as
    "GO_WITH_CAUTION -> DO_NOT_ADVISE"; both UIs render them structurally instead."""
    v = _verdict(downgraded_from="GO_WITH_CAUTION", ceiling_applied=True)
    for kind in ("ADVISORY", "INFORMATIONAL"):
        text = template_answer(v, "en", extras=[FINDING], answer_kind=kind)
        for code in ("GO_WITH_CAUTION", "DO_NOT_ADVISE", "_"):
            assert code not in text, f"{code!r} leaked into {kind} prose: {text}"


def test_unverified_contact_numbers_never_reach_the_prose() -> None:
    """A demo-directory number that gets read aloud and dialled in an emergency is the
    worst failure of the abstention path."""
    text = template_answer(_verdict(), "en", answer_kind="ADVISORY")
    assert "0123 456" not in text


# ---------------------------------------------------------------------------------------
# as_sentences — the duplication and run-on fixes
# ---------------------------------------------------------------------------------------


def test_every_fragment_gets_exactly_one_terminator() -> None:
    """Unterminated tool summaries used to run into the next sentence: "... 0.23 nm
    (WARN) Nearest landing centre: ..."."""
    assert as_sentences(["one", "two.", "three!  "]) == ["one.", "two.", "three!"]


def test_repeated_fragments_are_said_once() -> None:
    out = as_sentences(["Clear of all fences.", "clear of all fences", " Clear of all fences. "])
    assert out == ["Clear of all fences."]


def test_blank_fragments_are_dropped() -> None:
    assert as_sentences(["", "   ", None or "", "real"]) == ["real."]


# ---------------------------------------------------------------------------------------
# enforce_answer_contract — the model path's own invariant guard
# ---------------------------------------------------------------------------------------


def test_a_model_answer_that_drops_the_handoff_gets_it_back() -> None:
    """Invariant 2: DO_NOT_ADVISE hands off to a named human authority. The template path
    never lost it; the model path could, because only the *editor* pass was guarded."""
    written = "Do not go. The closest vessels are FB-01 and FB-05, both at 12.31 nm."
    out, repairs = enforce_answer_contract(
        written, verdict=_verdict(), language="en", answer_kind="INFORMATIONAL"
    )
    assert "Rameswaram Fishing Harbour" in out
    assert "named handoff restored" in repairs


def test_a_model_answer_that_never_states_the_verdict_gets_it_back() -> None:
    written = "The closest vessels are FB-01 and FB-05, both at 12.31 nm."
    out, repairs = enforce_answer_contract(
        written, verdict=_verdict(), language="en", answer_kind="INFORMATIONAL"
    )
    assert "Do not go" in out
    assert "verdict wording restored" in repairs
    # And the handoff repair still runs on the already-repaired text.
    assert "Rameswaram Fishing Harbour" in out


def test_an_informational_answer_opening_with_a_bare_verdict_is_reframed() -> None:
    written = "Do not go. The closest vessels are FB-01 and FB-05. Contact Rameswaram."
    out, repairs = enforce_answer_contract(
        written, verdict=_verdict(), language="en", answer_kind="INFORMATIONAL"
    )
    assert out.startswith("Safety note")
    assert "verdict reframed as context" in repairs


def test_an_advisory_answer_opening_with_the_verdict_is_left_alone() -> None:
    written = "Do not go. Seas are rough. Contact Rameswaram Fishing Harbour."
    out, repairs = enforce_answer_contract(
        written, verdict=_verdict(), language="en", answer_kind="ADVISORY"
    )
    assert out == written
    assert repairs == []


def test_a_complete_answer_is_never_touched() -> None:
    written = (
        "Safety note for this position and time — Do not go. Three vessels are inside. "
        "Contact Rameswaram Fishing Harbour — Harbour Master."
    )
    out, repairs = enforce_answer_contract(
        written, verdict=_verdict(), language="en", answer_kind="INFORMATIONAL"
    )
    assert out == written
    assert repairs == []


def test_a_vessel_named_after_the_port_does_not_satisfy_the_handoff_check() -> None:
    """This coast's boats are named after their harbour, so a first-token anchor on the
    authority name matched "Rameswaram FB-01" and let a DO_NOT_ADVISE answer ship with
    nobody to call."""
    written = "Rameswaram FB-01 and Rameswaram FB-05 are the closest, both at 12.31 nm."
    out, repairs = enforce_answer_contract(
        written, verdict=_verdict(), language="en", answer_kind="INFORMATIONAL"
    )
    assert "named handoff restored" in repairs
    assert "Rameswaram Fishing Harbour" in out


def test_answers_the_question_detects_a_model_that_wrote_about_the_verdict() -> None:
    findings = ["Three vessels are within 2.4 nm of the 1974 line."]
    assert not answers_the_question("Do not go. Conditions are against you.", findings)
    assert answers_the_question("The closest is 2.4 nm off the line.", findings)
    # Findings with no numbers cannot be checked this way and are not held against it.
    assert answers_the_question("anything", ["No fences are in range."])


def test_repair_never_leaks_an_unverified_contact_number() -> None:
    written = "The closest vessels are FB-01 and FB-05."
    out, _ = enforce_answer_contract(
        written, verdict=_verdict(), language="en", answer_kind="INFORMATIONAL"
    )
    assert "0123 456" not in out
