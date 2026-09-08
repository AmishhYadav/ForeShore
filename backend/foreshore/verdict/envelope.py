"""The decision envelope — a verdict extended over time, with the reason it bends.

Everything else in FORESHORE answers "can I go **now**". That is one instant, and it is
not the question a fisherman actually has. The questions are *when can I go*, *how long
have I got*, and *when must I turn back* — and none of them could be answered before this
module existed.

An envelope is the same deterministic verdict, evaluated once per forecast step across the
INCOIS OSF horizon, for one vessel class, carrying at every step the single **binding
constraint**: which one threshold or ceiling rule is saying no, and by how much.

Three properties this module deliberately does **not** have:

* **It does not re-implement the verdict.** Every step calls
  :func:`foreshore.verdict.engine.evaluate`, so the thresholds, the LLM-may-only-worsen
  rule and the advisory ceiling are the same code that answers a single-instant question.
  A divergence between "what FORESHORE says now" and "what the envelope says about now"
  is impossible by construction, not by discipline.
* **It does not extend the bulletin's authority.** Steps beyond the governing bulletin's
  validity come back ``DO_NOT_ADVISE`` from the ceiling, per ``DECISIONS.md`` D9. That is
  correct and is not a bug to route around: a 12-hour bulletin cannot authorise a trip on
  Thursday. The envelope reports where that boundary falls rather than hiding it.
* **It does not decide which constraint binds by inspecting prose.** The binding
  constraint is read off the structured :class:`~foreshore.verdict.engine.Threshold` and
  :class:`~foreshore.verdict.ceiling.CeilingResult` objects with a fixed, documented
  precedence, so the same inputs always name the same constraint — the trace a judge
  reads is identical run to run.
"""

from __future__ import annotations


from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Sequence

from ..models import Observation, VerdictLevel
from . import engine
from .ceiling import CeilingResult

# --------------------------------------------------------------------------------------
# Binding constraint
# --------------------------------------------------------------------------------------

#: Threshold precedence when two thresholds sit at the same level and both could be called
#: "the" reason. Fixed and explicit so the answer is deterministic — set/dict iteration
#: order must never decide what a fisherman is told. Ordered by how directly the quantity
#: capsizes a small boat: the wave that broaches you, then the wind driving it, then the
#: gust, then the steepness that makes a moderate sea dangerous, then visibility.
THRESHOLD_PRECEDENCE: tuple[str, ...] = (
    "significant_wave_height",
    "wind_speed",
    "wind_gust",
    "wave_steepness",
    "visibility",
)

#: Ceiling rule -> user-facing prose. The rule ids themselves are internal and must never
#: reach a screen (CLAUDE.md: no enum codes in user-facing strings).
CEILING_RULE_LABELS: dict[str, str] = {
    "missing_required_input": "a required input is missing",
    "bulletin_expired": "the governing coastal bulletin has expired",
    "bulletin_does_not_cover_departure": (
        "the governing coastal bulletin does not cover that departure time"
    ),
    "port_signal_hoisted": "a port warning signal is hoisted",
    "storm_surge_warning": "a storm-surge warning names this district",
    "kallakkadal_long_period_swell": "long-period swell is running into a shallow bay",
}

#: Threshold variable -> user-facing label. Same rule: `significant_wave_height` is an
#: identifier, "wave height" is what a person reads.
THRESHOLD_LABELS: dict[str, str] = {
    "significant_wave_height": "wave height",
    "wind_speed": "wind",
    "wind_gust": "gusts",
    "wave_steepness": "wave steepness",
    "visibility": "visibility",
}


@dataclass(frozen=True)
class BindingConstraint:
    """The single thing saying no, and how far from yes it is.

    ``key`` is internal (a threshold variable or a ceiling rule id) and is what the NavIC
    packet encodes as a small enum. ``label`` is the only field that may reach a screen.
    """

    kind: Literal["threshold", "ceiling", "none"]
    key: str
    label: str
    value: float | None = None
    limit: float | None = None
    unit: str | None = None
    #: How much the value must improve to reach the next better verdict level. Always
    #: reported as a positive magnitude — the direction is carried by ``label``'s phrasing.
    margin: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "limit": self.limit,
            "unit": self.unit,
            "margin": self.margin,
        }


def _threshold_margin(t: engine.Threshold) -> tuple[float | None, float | None]:
    """``(limit_just_crossed, magnitude_needed_to_get_back_under_it)``.

    For a normal higher-is-worse threshold the limit crossed is the caution limit at
    ``DO_NOT_ADVISE`` and the go limit at ``GO_WITH_CAUTION``. Visibility is the one
    lower-is-worse check in the engine (``engine.py``'s visibility block), so its margin
    runs the other way.
    """
    if t.value is None:
        return None, None
    if not t.higher_is_worse:
        limit = t.go_limit
        if limit is None:
            return None, None
        return limit, max(0.0, limit - t.value)
    limit = t.caution_limit if t.level == "DO_NOT_ADVISE" else t.go_limit
    if limit is None:
        return None, None
    return limit, max(0.0, t.value - limit)


def binding_constraint(outcome: engine.VerdictOutcome) -> BindingConstraint:
    """Which single constraint produced this verdict's level.

    Precedence, and the reasoning behind it:

    1. **The ceiling outranks every threshold.** If the advisory ceiling capped the
       verdict at the final level, the ceiling is what bound it — a hoisted port signal
       is the answer even when the sea is also rough, because the ceiling is the
       governing authority and the thresholds are FORESHORE's own reading. Telling a
       fisherman "the waves are high" when the real reason is "the port is closed" would
       be a true sentence and the wrong answer.
    2. Otherwise the worst threshold, tie-broken by :data:`THRESHOLD_PRECEDENCE`.
    3. A ``GO`` with nothing capping it has no binding constraint, and says so.
    """
    level = outcome.verdict.level
    ceiling: CeilingResult = outcome.ceiling

    if level != "GO" and ceiling.max_allowed == level and ceiling.rules_fired:
        for rule in ceiling.rules_fired:
            if rule in CEILING_RULE_LABELS:
                return BindingConstraint(
                    kind="ceiling", key=rule, label=CEILING_RULE_LABELS[rule]
                )

    if level != "GO":
        candidates = [t for t in outcome.thresholds if t.level == level]
        if candidates:
            def rank(t: engine.Threshold) -> int:
                try:
                    return THRESHOLD_PRECEDENCE.index(t.variable)
                except ValueError:
                    return len(THRESHOLD_PRECEDENCE)

            worst = min(candidates, key=rank)
            limit, margin = _threshold_margin(worst)
            return BindingConstraint(
                kind="threshold",
                key=worst.variable,
                label=THRESHOLD_LABELS.get(worst.variable, worst.variable.replace("_", " ")),
                value=worst.value,
                limit=limit,
                unit=worst.unit,
                margin=margin,
            )

    return BindingConstraint(kind="none", key="", label="nothing is limiting this trip")


# --------------------------------------------------------------------------------------
# The envelope
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvelopeStep:
    """One forecast step's verdict, and the reason for it."""

    when: datetime
    level: VerdictLevel
    binding: BindingConstraint
    #: `Provenance.provenance_id` for every observation this step was decided on, so a
    #: step in a chart can be traced back to the granule it came from.
    evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "when": self.when.isoformat(),
            "level": self.level,
            "binding": self.binding.to_dict(),
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class DecisionEnvelope:
    """A vessel's safe-operating window across the forecast horizon."""

    steps: tuple[EnvelopeStep, ...]
    vessel_class: str
    generated_at: datetime
    horizon_hours: int
    #: Last step at or better than `GO_WITH_CAUTION` before the first sustained closure.
    latest_safe_departure: datetime | None = None
    #: When a vessel already at sea must start back. Only set when a return duration is
    #: supplied — it is a function of *this* boat's passage time, not of the forecast.
    turn_back_by: datetime | None = None
    #: The next contiguous run of `GO` steps, if any.
    next_go_window: tuple[datetime, datetime] | None = None
    #: Steps the ceiling refused because the bulletin does not reach that far. Surfaced,
    #: never silently trimmed (CLAUDE.md invariant 4).
    beyond_bulletin_from: datetime | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "vessel_class": self.vessel_class,
            "generated_at": self.generated_at.isoformat(),
            "horizon_hours": self.horizon_hours,
            "latest_safe_departure": (
                self.latest_safe_departure.isoformat() if self.latest_safe_departure else None
            ),
            "turn_back_by": self.turn_back_by.isoformat() if self.turn_back_by else None,
            "next_go_window": (
                [self.next_go_window[0].isoformat(), self.next_go_window[1].isoformat()]
                if self.next_go_window
                else None
            ),
            "beyond_bulletin_from": (
                self.beyond_bulletin_from.isoformat() if self.beyond_bulletin_from else None
            ),
            "notes": list(self.notes),
        }


#: Levels at which a small boat may still legitimately put to sea. `DO_NOT_ADVISE` is the
#: closure, and it is the only one — there is no "borderline" tier, by invariant 2.
_WORKABLE: frozenset[str] = frozenset({"GO", "GO_WITH_CAUTION"})


def group_by_step(observations: Sequence[Observation]) -> list[tuple[datetime, list[Observation]]]:
    """Bucket a flat forecast series into one bucket per ``valid_time``, ascending.

    ``sources/incois_thredds.py::series`` returns every variable for every step in one
    flat list; the engine wants one step's worth at a time.
    """
    buckets: dict[datetime, list[Observation]] = {}
    for o in observations:
        buckets.setdefault(o.valid_time, []).append(o)
    return sorted(buckets.items(), key=lambda kv: kv[0])


def build_envelope(
    *,
    series: Sequence[Observation],
    base_context: engine.VerdictContext,
    vessel_class: str,
    generated_at: datetime,
    horizon_hours: int,
    static_observations: Sequence[Observation] = (),
    return_duration: timedelta | None = None,
    region: Any = None,
    vessel: Any = None,
) -> DecisionEnvelope:
    """Evaluate the verdict once per forecast step.

    ``base_context`` supplies everything that does not vary across the horizon — position,
    vessel class, and the governing bulletin's fields. Per step, its ``observations`` are
    replaced by that step's readings (plus any ``static_observations``, e.g. a bulletin-
    derived value that applies throughout) and ``when`` is set to the step's own time, so
    the ceiling evaluates *"does the bulletin cover this departure"* correctly per step —
    the distinction ``DECISIONS.md`` D9 was amended to preserve.

    ``now`` is deliberately taken from ``base_context`` unchanged for every step: the
    question "is the bulletin FORESHORE holds still current?" is about the wall clock, and
    it does not become a different question because we are asking about Thursday.
    """
    from dataclasses import replace as _replace

    steps: list[EnvelopeStep] = []
    beyond_from: datetime | None = None

    for when, step_obs in group_by_step(series):
        ctx = _replace(
            base_context,
            observations=list(step_obs) + list(static_observations),
            when=when,
        )
        outcome = engine.evaluate(ctx, region=region, vessel=vessel)
        binding = binding_constraint(outcome)
        if (
            beyond_from is None
            and "bulletin_does_not_cover_departure" in outcome.ceiling.rules_fired
        ):
            beyond_from = when
        steps.append(
            EnvelopeStep(
                when=when,
                level=outcome.verdict.level,
                binding=binding,
                evidence_ids=tuple(
                    o.provenance.provenance_id for o in step_obs if o.provenance
                ),
            )
        )

    notes: list[str] = []
    if beyond_from is not None:
        notes.append(
            "The governing coastal bulletin's validity ends inside this window. Steps "
            "after that point are shown as closed because no bulletin authorises them "
            "yet, not because the sea is forecast to be dangerous."
        )

    return DecisionEnvelope(
        steps=tuple(steps),
        vessel_class=vessel_class,
        generated_at=generated_at,
        horizon_hours=horizon_hours,
        latest_safe_departure=_latest_safe_departure(steps),
        turn_back_by=_turn_back_by(steps, return_duration),
        next_go_window=_next_go_window(steps),
        beyond_bulletin_from=beyond_from,
        notes=tuple(notes),
    )


def _latest_safe_departure(steps: Sequence[EnvelopeStep]) -> datetime | None:
    """The last workable step before the window first closes.

    Deliberately the end of the *first* workable run, not the last workable step anywhere
    in the horizon. A boat cannot depart into Thursday's clear weather through Wednesday's
    gale, so reporting Thursday as a "latest safe departure" would be an answer that
    reads as reassuring and is not survivable.
    """
    if not steps or steps[0].level not in _WORKABLE:
        return None
    last = steps[0].when
    for s in steps:
        if s.level not in _WORKABLE:
            break
        last = s.when
    return last


def _turn_back_by(
    steps: Sequence[EnvelopeStep], return_duration: timedelta | None
) -> datetime | None:
    """When a vessel already at sea must start back to be home before the window shuts.

    Requires a passage duration, because it is a property of *this* boat's speed and
    distance from shelter, not of the forecast. With none supplied this is ``None`` rather
    than a guess — a fabricated turn-back time is exactly the kind of unsourced number
    invariant 3 forbids.
    """
    if return_duration is None or not steps:
        return None
    closes_at: datetime | None = None
    for s in steps:
        if s.level not in _WORKABLE:
            closes_at = s.when
            break
    if closes_at is None:
        return None
    turn_back = closes_at - return_duration
    return turn_back if turn_back > steps[0].when else steps[0].when


def _next_go_window(steps: Sequence[EnvelopeStep]) -> tuple[datetime, datetime] | None:
    """The next contiguous run of unqualified ``GO`` steps.

    This is the half of the envelope that makes an advisory economically survivable: a
    system that only ever says no gets ignored, and the field research is explicit that
    fishermen need to be told when they *can* go.
    """
    start: datetime | None = None
    for s in steps:
        if s.level == "GO":
            if start is None:
                start = s.when
        elif start is not None:
            return (start, s.when)
    if start is not None:
        return (start, steps[-1].when)
    return None


__all__ = [
    "BindingConstraint",
    "EnvelopeStep",
    "DecisionEnvelope",
    "binding_constraint",
    "build_envelope",
    "group_by_step",
    "THRESHOLD_PRECEDENCE",
    "CEILING_RULE_LABELS",
    "THRESHOLD_LABELS",
]
