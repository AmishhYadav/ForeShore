"""Tests for PLAN.md Phase 7 item 4 — scenario exploration.

PLAN.md, verbatim: *"the PS asks twice for it ('explore scenarios', 'explore related
scenarios')... Support 'what if I leave at 04:00 instead of 06:00' as a re-plan over
the same evidence with a diffed verdict."* Unlike ``test_orchestrator.py``, this module
runs entirely in ``FORESHORE_MODE=fixture`` (the session-wide default from
``conftest.py``) — the fixture snapshot ``scripts/freeze_fixtures.py`` produces now
exists and covers the tools a safety-check plan needs, so there is no reason to pay for
live network calls to exercise deterministic time-parsing and diff logic.

Three things are under test:

1. ``planner.resolve_scenario_times`` — the pure trigger. Fires only on two explicit
   ``HH:MM`` times; a single time, no time, or an "earlier/later" utterance with no
   second instant to compare against all fall back to ``None`` (the ordinary
   single-answer path).
2. ``orchestrator.answer`` end to end for the PS's own example phrase produces exactly
   two options, earlier time first, each a complete independent verdict.
3. A caller-supplied ``when`` always suppresses scenario detection, even over text that
   names two times — an explicit instant means "answer for exactly this", never an
   unrequested comparison.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from foreshore.agents import orchestrator
from foreshore.agents.orchestrator import Query
from foreshore.agents.planner import resolve_scenario_times
from foreshore.models import UTC

_NOW = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# 1. resolve_scenario_times — pure function, no I/O.
# --------------------------------------------------------------------------------------


def test_two_explicit_times_return_sorted_regardless_of_order_in_text():
    assert resolve_scenario_times("what if I leave at 06:00 instead of 04:00", now=_NOW) == (
        _NOW.replace(hour=4, minute=0),
        _NOW.replace(hour=6, minute=0),
    )


def test_ps_example_phrase_verbatim():
    times = resolve_scenario_times("what if I leave at 04:00 instead of 06:00 tomorrow", now=_NOW)
    assert times is not None
    tomorrow = (_NOW + timedelta(days=1)).date()
    assert times[0].date() == tomorrow and times[0].hour == 4
    assert times[1].date() == tomorrow and times[1].hour == 6


def test_single_time_is_not_a_scenario():
    assert resolve_scenario_times("is it safe to leave at 06:00", now=_NOW) is None


def test_no_time_is_not_a_scenario():
    assert resolve_scenario_times("is it safe to go out now", now=_NOW) is None


def test_scenario_cue_words_alone_without_a_second_time_are_not_a_scenario():
    """"earlier"/"later"/"what if" still classify as the `scenario` *intent* (see
    planner.INTENT_CUES) so the trace's `intents` list reflects the utterance honestly —
    but with no second instant to compare against, there is nothing to diff."""
    assert resolve_scenario_times("what if I leave earlier", now=_NOW) is None


# --------------------------------------------------------------------------------------
# 2 & 3. orchestrator.answer end to end, fixture mode, no model.
# --------------------------------------------------------------------------------------


def test_scenario_question_produces_two_ordered_options_with_a_real_diff():
    query = Query(
        text="what if I leave at 04:00 instead of 06:00",
        surface="boat",
        use_model=False,
    )
    outcome = orchestrator.answer(query)

    assert outcome.scenario is not None
    options = outcome.scenario.options
    assert len(options) == 2
    assert options[0].when < options[1].when
    assert options[0].label == f"Leave at {options[0].when.strftime('%H:%M')}"
    assert options[1].label == f"Leave at {options[1].when.strftime('%H:%M')}"

    # Each option is a complete, independent answer — not a stub.
    for opt in options:
        assert opt.outcome.verdict is not None
        assert opt.outcome.verdict.level in ("GO", "GO_WITH_CAUTION", "DO_NOT_ADVISE")

    assert outcome.scenario.differences, "a scenario comparison must always say something"
    assert outcome.scenario.recommended_index in (0, 1)

    # The outer answer itself is a real, renderable QueryOutcome too (option A, with the
    # comparison attached) — a scenario question is never "verdict: null".
    assert outcome.verdict is not None
    assert outcome.verdict.level == options[0].outcome.verdict.level


def test_explicit_when_suppresses_scenario_detection_even_over_two_time_text():
    query = Query(
        text="what if I leave at 04:00 instead of 06:00",
        surface="boat",
        use_model=False,
        when=_NOW,
    )
    outcome = orchestrator.answer(query)
    assert outcome.scenario is None


def test_ordinary_query_never_carries_a_scenario():
    query = Query(text="is it safe to go out now", surface="boat", use_model=False)
    outcome = orchestrator.answer(query)
    assert outcome.scenario is None
    assert outcome.to_dict()["scenario"] is None
