"""Tool 19 — ``get_decision_envelope``.

Every other tool in this system answers "can I go **now**". This one answers the three
questions a fisherman actually has and nothing before ``verdict/envelope.py`` existed
could answer: *when can I go*, *how long have I got*, and *when must I turn back*.

This module is a thin, provenance-preserving wrapper around
:func:`foreshore.verdict.envelope.build_envelope`. It does not re-implement any part of
the decision logic — the per-step verdict, the binding-constraint precedence and the
three derived times all live in ``verdict/envelope.py`` (a fixed contract for this task)
and are reused exactly, never re-derived here.

Two responsibilities live here, and only here:

1. **One fetch for the whole horizon.** ``sources/incois_thredds.py::series`` returns
   every INCOIS OSF wave variable for every forecast step in a single flat list — one
   cold NetCDF grid fetch covers up to 56 steps x 3 h = 7 days. ``envelope.group_by_step``
   buckets it. A per-step point query would multiply the single most expensive fetch in
   this system (CLAUDE.md's own latency section) by up to 56x for no benefit, so this
   tool calls ``series(...)`` exactly once and never loops a point query over the
   horizon.
2. **Wiring the governing bulletin in**, not deriving it: the bulletin's sea-condition,
   port-signal and storm-surge fields are pulled from whatever evidence the rest of this
   turn already gathered (via the evidence bus tool 15 owns), reusing
   ``tools.verdict_tools._extract_bulletin_fields`` and ``_make_handoff_provider``
   rather than re-parsing the bulletin a second way. When no evidence bus entry exists,
   this falls back to one direct call to ``get_governing_advisory`` — the same fallback
   shape ``evaluate_verdict`` already uses.

A missing bulletin is not a crash. ``verdict.ceiling.compute_ceiling`` already treats a
``None`` ``bulletin_provenance`` as "missing_required_input" and returns
``DO_NOT_ADVISE`` for every step -- correct, not a bug to route around. This tool still
flags it explicitly (``partial=True, missing=["imd_coastal_bulletin"]``) so the gap is
surfaced rather than left for a reader to infer from an all-closed chart (CLAUDE.md
invariant 4: staleness and gaps are surfaced, never hidden).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Sequence

from ..config import load_region, load_vessels
from ..models import Observation, ToolResult, utcnow
from ..verdict import engine
from ..verdict.envelope import DecisionEnvelope, EnvelopeStep, build_envelope, group_by_step
from .registry import latlon_schema, registry
from .verdict_tools import _extract_bulletin_fields, _make_handoff_provider, evidence_for

#: The full INCOIS OSF horizon: 56 steps x 3 h. Matches the OSF coastal wave nest exactly
#: (CLAUDE.md), so a default call reads everything the model publishes in one fetch.
_DEFAULT_HORIZON_HOURS = 168

#: Same four variables ``get_sea_state`` (tool 2) requests from the "wave" product --
#: named consistently so a step's evidence reads the same way across both tools.
_SERIES_VARIABLES: tuple[str, ...] = (
    "significant_wave_height", "swell_wave_height", "wave_period", "swell_wave_period",
)

#: internal missing-key -> user-facing phrase. Never let a source id (an underscore
#: identifier) leak into a summary string.
_MISSING_LABELS: dict[str, str] = {
    "incois_osf_wave": "the INCOIS wave forecast",
    "incois_osf_wave_horizon": (
        "full coverage of the INCOIS wave forecast (only part of the requested horizon "
        "was returned)"
    ),
    "imd_coastal_bulletin": "the governing coastal bulletin",
}


def _iso(dt: Any) -> str:
    """Microsecond-free ISO-8601. A fractional second makes the unsourced-number audit's
    clock/date skip rule ambiguous (a run of digits either side of a bare '.'), so it is
    stripped before any timestamp reaches user-facing prose."""
    return dt.replace(microsecond=0).isoformat()


def _describe_missing(missing: Sequence[str]) -> str:
    labels = [_MISSING_LABELS.get(m, m) for m in missing]
    return f" Unavailable: {', '.join(labels)}."


def _fetch_series(
    lat: float, lon: float, horizon_hours: int
) -> tuple[list[Observation], str | None]:
    """The one and only fetch for the whole horizon. Never raises."""
    try:
        from ..sources.incois_thredds import IncoisThredds
    except Exception as exc:  # noqa: BLE001 - adapter module itself unavailable
        return [], f"INCOIS OSF adapter could not be loaded ({type(exc).__name__}: {exc})"
    try:
        obs = IncoisThredds().series(
            "wave", lat, lon, variables=list(_SERIES_VARIABLES), hours=horizon_hours
        )
    except Exception as exc:  # noqa: BLE001 - transport/parse failure must not fail the tool
        return [], f"INCOIS OSF wave series query failed ({type(exc).__name__}: {exc})"
    if not obs:
        return [], "INCOIS OSF wave nest returned no data across this horizon for this position"
    return obs, None


def _abstain(summary: str, *, missing: list[str]) -> ToolResult:
    return ToolResult(
        tool="get_decision_envelope",
        ok=True,
        partial=True,
        observations=[],
        # Shaped like a real envelope's dict (empty steps, no derived times) so a
        # renderer that always reads payload["steps"] degrades to "nothing to chart"
        # rather than a KeyError.
        payload={
            "steps": [], "latest_safe_departure": None, "turn_back_by": None,
            "next_go_window": None, "beyond_bulletin_from": None,
        },
        missing=missing,
        summary=summary,
    )


def _build_summary(
    envelope: DecisionEnvelope,
    vessel_label: str,
    missing: list[str],
    bulletin_missing: bool,
) -> str:
    """User-facing prose, leading with the actionable answer.

    Never states a threshold limit or a margin as a raw number -- those are vessel
    config constants and arithmetic on them, not values traced to an Observation, and
    invariant 3 forbids an unsourced number in an answer. Timestamps are safe (the
    unsourced-number audit's own clock/date skip rule exempts them) and are the only
    numbers this prose ever states.
    """
    steps: Sequence[EnvelopeStep] = envelope.steps
    first = steps[0]
    closing = next((s for s in steps if s.level == "DO_NOT_ADVISE"), None)

    lead = (
        f"Decision window for a {vessel_label}, generated at {_iso(envelope.generated_at)} "
        f"and evaluated through {_iso(steps[-1].when)}: "
    )

    if bulletin_missing:
        body = (
            "no governing coastal bulletin could be read, so every step in this window "
            "is shown as closed -- a verdict cannot be authorised against a bulletin "
            "that could not be read."
        )
    elif first.level in ("GO", "GO_WITH_CAUTION"):
        if envelope.latest_safe_departure is not None and closing is not None:
            body = (
                f"conditions are workable now and stay that way until "
                f"{_iso(envelope.latest_safe_departure)}, when {closing.binding.label} "
                "closes the window."
            )
        else:
            body = (
                "conditions stay workable across the whole evaluated period; nothing is "
                "currently limiting this trip."
            )
    else:
        body = f"conditions are closed right now: {first.binding.label}."

    parts = [lead + body]

    if envelope.next_go_window is not None:
        start, end = envelope.next_go_window
        parts.append(
            f"The next clear window opens at {_iso(start)} and holds until {_iso(end)}."
        )
    elif closing is not None:
        parts.append("No further unqualified clear window is forecast within this horizon.")

    if envelope.turn_back_by is not None:
        parts.append(
            "With the return passage time given, turn back by "
            f"{_iso(envelope.turn_back_by)} to be sheltered before the window shuts."
        )

    if envelope.beyond_bulletin_from is not None:
        parts.append(
            f"Steps from {_iso(envelope.beyond_bulletin_from)} onward are shown as "
            "closed only because the governing bulletin's own validity does not reach "
            "that far, not because the sea is forecast to be dangerous."
        )

    if missing:
        parts.append(_describe_missing(missing))

    return " ".join(parts)


@registry.tool(
    name="get_decision_envelope",
    number=19,
    description=(
        "Evaluate the deterministic FORESHORE verdict once per forecast step across the "
        "INCOIS OSF horizon (up to 7 days, 3-hour steps) for one vessel class, and "
        "return the single binding constraint at each step plus three derived times: "
        "the latest safe departure before the window first closes, the next clear GO "
        "window, and (when a return passage duration is given) the latest turn-back "
        "time. Answers 'when can I go', 'how long have I got' and 'when must I turn "
        "back' -- questions a single-instant verdict cannot answer. Steps beyond the "
        "governing bulletin's 12-hour validity come back closed because no bulletin "
        "authorises them yet, not because the sea is dangerous; that boundary is "
        "reported explicitly, never hidden."
    ),
    schema=latlon_schema(
        vessel_class={
            "type": ["string", "null"],
            "description": (
                "Vessel class id from config/vessels.yaml (e.g. 'small_motorised'). "
                "Defaults to the region's configured default class when omitted."
            ),
        },
        horizon_hours={
            "type": ["integer", "null"],
            "description": (
                "Forecast horizon to evaluate, in hours, over the INCOIS OSF's own "
                "3-hour steps. Defaults to 168 (7 days, the full OSF horizon)."
            ),
        },
        return_duration_hours={
            "type": ["number", "null"],
            "description": (
                "This vessel's passage time back to shelter, in hours. When given, the "
                "envelope also reports the latest turn-back time for a vessel already "
                "at sea. Omit for a departure-planning question."
            ),
        },
        evidence_query_id={
            "type": ["string", "null"],
            "description": (
                "Query id whose already-gathered evidence (the governing bulletin, from "
                "an earlier tool call this turn) should be reused instead of "
                "re-fetching it."
            ),
        },
    ),
    specialists=("PlanningAgent", "RiskAssessment"),
    reads_sources=("incois_osf_wave", "imd_coastal_bulletin"),
    cost="slow",
)
def get_decision_envelope(
    lat: float,
    lon: float,
    vessel_class: str | None = None,
    horizon_hours: int | None = None,
    return_duration_hours: float | None = None,
    evidence_query_id: str | None = None,
) -> ToolResult:
    """The decision envelope for ``(lat, lon)``: one verdict per forecast step, and the
    three derived times that turn it into an operational plan.

    Never raises. Fetches the whole horizon with exactly one
    ``IncoisThredds.series(...)`` call -- see the module docstring; this is the
    efficiency contract the tool exists to hold, and it is the easiest thing about this
    tool to regress. A missing or empty series abstains (``ok=True, partial=True``, no
    steps). A missing governing bulletin does not abstain -- ``build_envelope`` still
    runs (every step correctly comes back ``DO_NOT_ADVISE`` via the ceiling's own
    "missing_required_input" rule) and the gap is flagged instead
    (``partial=True, missing=["imd_coastal_bulletin"]``), never silently hidden.
    """
    generated_at = utcnow()
    horizon = max(3, int(horizon_hours) if horizon_hours else _DEFAULT_HORIZON_HOURS)
    return_duration = (
        timedelta(hours=float(return_duration_hours))
        if return_duration_hours is not None and return_duration_hours > 0
        else None
    )

    region = load_region()
    vessel = load_vessels().get(vessel_class)

    # -- the one expensive fetch: the whole horizon, in a single series() call ---------
    series_obs, series_reason = _fetch_series(lat, lon, horizon)

    missing: list[str] = []
    if series_reason is not None:
        missing.append("incois_osf_wave")

    if not series_obs:
        return _abstain(
            "No INCOIS OSF wave forecast could be read for this position across the "
            "requested horizon, so no decision window can be built. Abstaining rather "
            "than guessing." + _describe_missing(["incois_osf_wave"]),
            missing=["incois_osf_wave"],
        )

    steps_available = len(group_by_step(series_obs))
    expected_steps = max(1, horizon // 3)
    if steps_available < max(1, expected_steps // 2):
        missing.append("incois_osf_wave_horizon")

    # -- the governing bulletin: reused from the evidence bus, or one direct fetch -----
    bulletin_observations = evidence_for(evidence_query_id)
    sources_used = ["evidence_bus"] if bulletin_observations else []
    if not bulletin_observations and "get_governing_advisory" in registry:
        adv = registry.call("get_governing_advisory", {"lat": lat, "lon": lon})
        sources_used = ["get_governing_advisory"]
        bulletin_observations = list(adv.observations)

    bulletin_fields = _extract_bulletin_fields(bulletin_observations)
    bulletin_missing = bulletin_fields["bulletin_provenance"] is None
    if bulletin_missing:
        missing.append("imd_coastal_bulletin")

    base_context = engine.VerdictContext(
        lat=lat,
        lon=lon,
        observations=[],
        vessel_class_id=vessel.class_id,
        now=generated_at,
        sea_condition=bulletin_fields["sea_condition"],
        port_signal=bulletin_fields["port_signal"],
        storm_surge_warning=bulletin_fields["storm_surge_warning"],
        coast_block=bulletin_fields["coast_block"],
        bulletin_provenance=bulletin_fields["bulletin_provenance"],
        bulletin_valid_from=bulletin_fields["bulletin_valid_from"],
        bulletin_valid_to=bulletin_fields["bulletin_valid_to"],
        handoff_provider=_make_handoff_provider(lat, lon),
    )

    envelope = build_envelope(
        series=series_obs,
        base_context=base_context,
        vessel_class=vessel.class_id,
        generated_at=generated_at,
        horizon_hours=horizon,
        return_duration=return_duration,
        region=region,
        vessel=vessel,
    )

    observations = list(series_obs) + [o for o in bulletin_observations if o is not None]

    payload = envelope.to_dict()
    payload["vessel_label"] = vessel.label_en
    payload["bulletin_missing"] = bulletin_missing
    payload["sources_used"] = sources_used

    return ToolResult(
        tool="get_decision_envelope",
        ok=True,
        observations=observations,
        payload=payload,
        summary=_build_summary(envelope, vessel.label_en, missing, bulletin_missing),
        partial=bool(missing),
        missing=missing,
    )


__all__ = ["get_decision_envelope"]
