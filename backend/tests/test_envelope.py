"""Decision-envelope logic.

Covers the parts that decide what a fisherman is told and must therefore be deterministic:
which constraint is named as binding, and where the three derived times fall. The
per-step call into `verdict.engine.evaluate` is not re-tested here — `test_ceiling.py` and
`test_provenance.py` already hold that contract, and the envelope's whole design claim is
that it reuses it rather than reimplementing it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from foreshore.verdict import engine
from foreshore.verdict.ceiling import CeilingResult
from foreshore.verdict.douglas import parse_sea_condition
from foreshore.verdict.envelope import (
    CEILING_RULE_LABELS,
    THRESHOLD_LABELS,
    BindingConstraint,
    EnvelopeStep,
    _latest_safe_departure,
    _next_go_window,
    _turn_back_by,
    binding_constraint,
    group_by_step,
)
from foreshore.models import Verdict

T0 = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)


def _threshold(variable, value, go, caution, level, *, higher_is_worse=True, unit="m"):
    return engine.Threshold(
        variable=variable, value=value, unit=unit, go_limit=go, caution_limit=caution,
        level=level, reason=f"{variable} {value}", observation=None,
        higher_is_worse=higher_is_worse,
    )


def _outcome(level, thresholds, *, ceiling_max=None, rules=()):
    return engine.VerdictOutcome(
        verdict=Verdict(level=level, reasons=[], evidence=[], lat=9.3, lon=79.2),
        thresholds=list(thresholds),
        ceiling=CeilingResult(
            max_allowed=ceiling_max or level,
            reading=parse_sea_condition("MODERATE"),
            rules_fired=list(rules),
        ),
        disagreements=[],
        derived=[],
    )


def _step(hours, level):
    return EnvelopeStep(when=T0 + timedelta(hours=hours), level=level,
                        binding=BindingConstraint("none", "", "x"))


# -- binding constraint ----------------------------------------------------------------


def test_the_ceiling_outranks_a_threshold_that_reached_the_same_level():
    """A hoisted port signal is the answer even when the sea is also rough.

    Both are true; only one is the governing authority. Telling a fisherman "the waves are
    high" when the real reason is "the port is closed" is a true sentence and the wrong
    answer — he can read the sea himself, he cannot read the port office.
    """
    out = _outcome(
        "GO_WITH_CAUTION",
        [_threshold("significant_wave_height", 1.6, 1.25, 2.5, "GO_WITH_CAUTION")],
        rules=["port_signal_hoisted"],
    )
    b = binding_constraint(out)
    assert b.kind == "ceiling"
    assert b.key == "port_signal_hoisted"
    assert b.label == CEILING_RULE_LABELS["port_signal_hoisted"]


def test_ties_break_on_the_documented_precedence_not_on_list_order():
    """Two thresholds at the same level must always name the same one."""
    wind_first = _outcome("DO_NOT_ADVISE", [
        _threshold("wind_speed", 30, 15, 22, "DO_NOT_ADVISE", unit="kn"),
        _threshold("significant_wave_height", 3.0, 1.25, 2.5, "DO_NOT_ADVISE"),
    ])
    waves_first = _outcome("DO_NOT_ADVISE", [
        _threshold("significant_wave_height", 3.0, 1.25, 2.5, "DO_NOT_ADVISE"),
        _threshold("wind_speed", 30, 15, 22, "DO_NOT_ADVISE", unit="kn"),
    ])
    assert binding_constraint(wind_first).key == "significant_wave_height"
    assert binding_constraint(waves_first).key == "significant_wave_height"


def test_margin_is_the_distance_back_under_the_limit_just_crossed():
    """DO_NOT_ADVISE measures back to the caution limit; caution measures to the go limit."""
    stopped = binding_constraint(_outcome(
        "DO_NOT_ADVISE", [_threshold("significant_wave_height", 3.0, 1.25, 2.5, "DO_NOT_ADVISE")]
    ))
    assert stopped.limit == 2.5
    assert stopped.margin == 0.5

    cautioned = binding_constraint(_outcome(
        "GO_WITH_CAUTION", [_threshold("significant_wave_height", 1.62, 1.25, 2.5, "GO_WITH_CAUTION")]
    ))
    assert cautioned.limit == 1.25
    assert round(cautioned.margin, 2) == 0.37


def test_visibility_margin_runs_the_other_way():
    """Visibility is the engine's one lower-is-worse check."""
    b = binding_constraint(_outcome("GO_WITH_CAUTION", [
        _threshold("visibility", 1200, 2000, None, "GO_WITH_CAUTION",
                   higher_is_worse=False, unit="m"),
    ]))
    assert b.margin == 800
    assert b.label == THRESHOLD_LABELS["visibility"]


def test_a_clear_go_names_no_binding_constraint():
    b = binding_constraint(_outcome(
        "GO", [_threshold("significant_wave_height", 0.6, 1.25, 2.5, "GO")]
    ))
    assert b.kind == "none"


def test_no_binding_label_leaks_an_identifier_or_an_enum_code():
    """CLAUDE.md: no enum codes or variable identifiers in user-facing strings."""
    banned = ("DO_NOT_ADVISE", "GO_WITH_CAUTION", "BREACH", "_")
    for label in list(CEILING_RULE_LABELS.values()) + list(THRESHOLD_LABELS.values()):
        for token in banned:
            assert token not in label, f"{label!r} leaks {token!r}"


# -- derived times ---------------------------------------------------------------------


def test_latest_safe_departure_is_the_end_of_the_first_workable_run():
    """Not the last workable step anywhere in the horizon.

    A boat cannot depart into Thursday's clear weather through Wednesday's gale, so
    reporting the later run would read as reassuring and not be survivable.
    """
    steps = [_step(0, "GO"), _step(3, "GO_WITH_CAUTION"), _step(6, "DO_NOT_ADVISE"),
             _step(9, "GO"), _step(12, "GO")]
    assert _latest_safe_departure(steps) == T0 + timedelta(hours=3)


def test_no_safe_departure_when_the_window_is_shut_right_now():
    assert _latest_safe_departure([_step(0, "DO_NOT_ADVISE"), _step(3, "GO")]) is None


def test_next_go_window_finds_the_run_after_a_closure():
    steps = [_step(0, "DO_NOT_ADVISE"), _step(3, "GO"), _step(6, "GO"),
             _step(9, "GO_WITH_CAUTION")]
    assert _next_go_window(steps) == (T0 + timedelta(hours=3), T0 + timedelta(hours=9))


def test_next_go_window_is_none_when_the_horizon_never_opens():
    assert _next_go_window([_step(0, "DO_NOT_ADVISE"), _step(3, "GO_WITH_CAUTION")]) is None


def test_turn_back_by_subtracts_the_passage_from_the_closure():
    steps = [_step(0, "GO"), _step(3, "GO"), _step(6, "DO_NOT_ADVISE")]
    assert _turn_back_by(steps, timedelta(hours=2)) == T0 + timedelta(hours=4)


def test_turn_back_by_is_none_without_a_passage_duration():
    """A fabricated turn-back time is exactly the unsourced number invariant 3 forbids."""
    steps = [_step(0, "GO"), _step(3, "DO_NOT_ADVISE")]
    assert _turn_back_by(steps, None) is None


def test_turn_back_by_never_reports_a_time_already_past():
    """A boat whose passage home is longer than the window left must be told to go now."""
    steps = [_step(0, "GO"), _step(3, "DO_NOT_ADVISE")]
    assert _turn_back_by(steps, timedelta(hours=9)) == T0


def test_turn_back_by_is_none_when_the_window_never_closes():
    assert _turn_back_by([_step(0, "GO"), _step(3, "GO")], timedelta(hours=1)) is None


# -- grouping --------------------------------------------------------------------------


def test_group_by_step_buckets_a_flat_series_in_time_order():
    class _O:
        def __init__(self, t):
            self.valid_time = t

    later, earlier = _O(T0 + timedelta(hours=3)), _O(T0)
    grouped = group_by_step([later, earlier, _O(T0)])
    assert [t for t, _ in grouped] == [T0, T0 + timedelta(hours=3)]
    assert len(grouped[0][1]) == 2
