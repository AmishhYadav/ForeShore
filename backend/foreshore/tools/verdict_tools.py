"""Tool 15 — ``evaluate_verdict``.

This module is a thin, provenance-preserving wrapper around the deterministic verdict
engine (:mod:`foreshore.verdict.engine`) and the advisory ceiling
(:mod:`foreshore.verdict.ceiling`). It does not implement any threshold or ceiling logic
itself — if a comparison against a vessel limit or a bulletin rule needs to be written
here, that is a sign the logic belongs in the (owned-elsewhere, safety-critical)
``verdict`` package instead.

Two responsibilities live here:

1. **The evidence bus.** A single query typically calls several tools (advisory, sea
   state, weather, ...) before it is ready to evaluate a verdict. ``record_evidence`` /
   ``evidence_for`` / ``clear_evidence`` let the agent runtime accumulate the
   observations gathered across those calls under one ``query_id`` so
   ``evaluate_verdict`` can consume all of them at once instead of re-fetching. When no
   evidence has been recorded for the id (or none is given at all), the tool falls back
   to gathering a minimal set itself.
2. **Wiring**, not deciding: build a ``VerdictContext`` from whatever evidence is on
   hand, wire a handoff provider that asks the (possibly not-yet-available)
   ``nearest_harbour`` tool for a named landing centre, and call
   ``verdict.engine.evaluate``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Sequence

from ..models import Handoff, Observation, Provenance, ToolResult, utcnow
from .registry import latlon_schema, registry

# --------------------------------------------------------------------------------------
# Evidence bus — module-level, process-lifetime, keyed by query id.
# --------------------------------------------------------------------------------------

_EVIDENCE: dict[str, list[Observation]] = {}


def record_evidence(query_id: str, observations: Sequence[Observation]) -> None:
    """Append ``observations`` (from any tool call) to the bucket for ``query_id``.

    Called by the agent runtime after each tool call in a turn so later tools in the
    same query — chiefly ``evaluate_verdict`` — can see everything gathered so far
    without re-fetching it.
    """
    if not query_id:
        return
    bucket = _EVIDENCE.setdefault(query_id, [])
    bucket.extend(o for o in observations if isinstance(o, Observation))


def evidence_for(query_id: str | None) -> list[Observation]:
    """Everything recorded for ``query_id`` so far, or ``[]`` if none / not given."""
    if not query_id:
        return []
    return list(_EVIDENCE.get(query_id, []))


def clear_evidence(query_id: str) -> None:
    """Drop the bucket for ``query_id``. Called once a query is fully answered."""
    _EVIDENCE.pop(query_id, None)
    _OUTCOMES.pop(query_id, None)


# --------------------------------------------------------------------------------------
# Outcome handoff — the tool boundary serialises, the orchestrator needs the object.
# --------------------------------------------------------------------------------------

#: The last :class:`~foreshore.verdict.engine.VerdictOutcome` this tool produced, keyed by
#: the ``evidence_query_id`` it was called with (and under ``"__last__"`` regardless).
#: A ``ToolResult`` payload is JSON-shaped by contract, so the live ``Verdict`` — with its
#: Observation evidence, its ceiling provenance and its Handoff — cannot travel inside it.
#: The synthesis layer needs that object to render the evidence panel and to apply the
#: ceiling copy, so it is handed over here rather than reconstructed from a dict, which
#: would silently drop provenance.
_OUTCOMES: dict[str, Any] = {}


def last_outcome(query_id: str | None = None) -> Any | None:
    """The VerdictOutcome for ``query_id``, or the most recent one when not given."""
    if query_id and query_id in _OUTCOMES:
        return _OUTCOMES[query_id]
    return _OUTCOMES.get("__last__")


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _parse_when(when: str | None) -> datetime | None:
    if not when:
        return None
    try:
        return datetime.fromisoformat(when.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _handoff_from_payload(raw: Any) -> Handoff | None:
    """Best-effort, never-raising conversion of a tool payload's ``handoff`` dict.

    ``nearest_harbour`` (tool 14) is owned elsewhere and may not exist yet, or may
    shape its payload slightly differently than expected here. Any malformed or
    partial dict degrades to ``None`` (the caller then falls back to the regional
    Coast Guard handoff in ``verdict.ceiling.regional_handoff``) rather than raising —
    a broken handoff must never take down the abstention path it exists to serve.
    """
    if not isinstance(raw, dict):
        return None
    try:
        prov: Provenance | None = None
        prov_dict = raw.get("provenance")
        if isinstance(prov_dict, dict):
            try:
                prov = Provenance(
                    source_id=str(prov_dict.get("source_id", "nearest_harbour")),
                    source_name=str(prov_dict.get("source_name", "Nearest landing centre")),
                    authority=prov_dict.get("authority", "derived"),
                    url=str(prov_dict.get("url", "")),
                    acquired_at=_parse_dt(prov_dict.get("acquired_at")) or utcnow(),
                    issued_at=_parse_dt(prov_dict.get("issued_at")),
                    valid_from=_parse_dt(prov_dict.get("valid_from")),
                    valid_to=_parse_dt(prov_dict.get("valid_to")),
                    spatial_resolution_m=prov_dict.get("spatial_resolution_m"),
                    is_derived=bool(prov_dict.get("is_derived", False)),
                    notes=prov_dict.get("notes"),
                )
            except Exception:
                prov = None
        authority_type = raw.get("authority_type") or "landing_centre"
        if authority_type not in (
            "landing_centre", "coast_guard", "fisheries_office", "port_office",
        ):
            authority_type = "landing_centre"
        authority_name = raw.get("authority_name")
        if not authority_name:
            return None  # a handoff with no named authority is not a named handoff
        return Handoff(
            reason=str(raw.get("reason") or "Nearest landing centre for handoff."),
            authority_name=str(authority_name),
            authority_type=authority_type,  # type: ignore[arg-type]
            contact=raw.get("contact"),
            lat=raw.get("lat"),
            lon=raw.get("lon"),
            distance_nm=raw.get("distance_nm"),
            provenance=prov,
        )
    except Exception:
        return None


def _make_handoff_provider(lat: float, lon: float) -> Callable[[], Handoff | None]:
    def provider() -> Handoff | None:
        try:
            if "nearest_harbour" not in registry:
                return None
            result = registry.call("nearest_harbour", {"lat": lat, "lon": lon})
            if not result.ok:
                return None
            return _handoff_from_payload(result.payload.get("handoff"))
        except Exception:
            return None

    return provider


def _extract_bulletin_fields(
    observations: Sequence[Observation],
) -> dict[str, Any]:
    """Pull the IMD bulletin's sea-condition / port-signal / storm-surge fields (and
    their shared provenance and validity window) out of whatever evidence is on hand,
    regardless of which tool produced it."""
    sea_obs = next((o for o in observations if o.variable == "sea_condition"), None)
    port_obs = next((o for o in observations if o.variable == "port_signal"), None)
    surge_obs = next(
        (o for o in observations if o.variable == "storm_surge_tidal_warning"), None
    )

    coast_block = None
    for o in (sea_obs, port_obs, surge_obs):
        if o is not None and o.qualifiers.get("coast_block"):
            coast_block = o.qualifiers.get("coast_block")
            break

    return {
        "sea_condition": sea_obs.value if isinstance(getattr(sea_obs, "value", None), str) else None,
        "port_signal": port_obs.value if isinstance(getattr(port_obs, "value", None), str) else None,
        "storm_surge_warning": (
            surge_obs.value if isinstance(getattr(surge_obs, "value", None), str) else None
        ),
        "coast_block": coast_block,
        "bulletin_provenance": sea_obs.provenance if sea_obs else None,
        "bulletin_valid_from": sea_obs.provenance.valid_from if sea_obs else None,
        "bulletin_valid_to": sea_obs.provenance.valid_to if sea_obs else None,
    }


# --------------------------------------------------------------------------------------
# Tool
# --------------------------------------------------------------------------------------


@registry.tool(
    name="evaluate_verdict",
    number=15,
    description=(
        "Run the deterministic FORESHORE verdict engine and advisory ceiling for a "
        "position, vessel class and time. Returns exactly one of GO / GO_WITH_CAUTION / "
        "DO_NOT_ADVISE, the reasons, whether the advisory ceiling downgraded the result "
        "and from what, and — whenever it abstains — a named human handoff. Reuses "
        "evidence already gathered earlier in this query when 'evidence_query_id' is "
        "given, otherwise gathers a minimal evidence set itself. Call this last, after "
        "the governing advisory (and sea state / weather, when available) have been "
        "retrieved for this position. This tool never invents a threshold comparison — "
        "it only wires already-sourced evidence into the verdict engine."
    ),
    schema=latlon_schema(
        vessel_class={
            "type": "string",
            "description": (
                "Vessel class id from config/vessels.yaml (e.g. 'small_motorised', "
                "'fibreglass_catamaran'). Defaults to the region's configured default "
                "class when omitted."
            ),
        },
        when={
            "type": "string",
            "description": "ISO 8601 timestamp to evaluate at. Defaults to now.",
        },
        evidence_query_id={
            "type": "string",
            "description": (
                "Query id whose already-gathered evidence (recorded by earlier tool "
                "calls in this turn via the evidence bus) should be reused instead of "
                "re-fetching."
            ),
        },
    ),
    specialists=("RiskAssessment",),
    reads_sources=("imd_coastal_bulletin",),
    cost="fast",
)
def evaluate_verdict(
    lat: float,
    lon: float,
    vessel_class: str | None = None,
    when: str | None = None,
    evidence_query_id: str | None = None,
) -> ToolResult:
    """Deterministic GO / GO_WITH_CAUTION / DO_NOT_ADVISE for ``(lat, lon)``.

    Never re-implements a threshold or ceiling comparison — everything here delegates
    to :func:`foreshore.verdict.engine.evaluate`, which applies the advisory ceiling
    last, over the top of whatever the deterministic thresholds (and an optional LLM
    proposal, unused by this tool) produced.
    """
    try:
        from ..verdict import engine
    except Exception as exc:
        return ToolResult(
            tool="evaluate_verdict",
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            summary=f"Verdict engine unavailable ({exc}); cannot evaluate a verdict.",
            missing=["verdict_engine"],
        )

    when_dt = _parse_when(when)

    observations: list[Observation] = evidence_for(evidence_query_id)
    sources_used: list[str] = ["evidence_bus"] if observations else []

    if not observations:
        sources_used = []
        if "get_governing_advisory" in registry:
            adv = registry.call("get_governing_advisory", {"lat": lat, "lon": lon})
            sources_used.append("get_governing_advisory")
            observations.extend(adv.observations)
        if "get_sea_state" in registry:
            ss = registry.call("get_sea_state", {"lat": lat, "lon": lon})
            sources_used.append("get_sea_state")
            observations.extend(ss.observations)
        if "get_weather" in registry:
            wx = registry.call("get_weather", {"lat": lat, "lon": lon})
            sources_used.append("get_weather")
            observations.extend(wx.observations)

    bulletin_fields = _extract_bulletin_fields(observations)

    ctx = engine.VerdictContext(
        lat=lat,
        lon=lon,
        observations=observations,
        vessel_class_id=vessel_class,
        when=when_dt,
        sea_condition=bulletin_fields["sea_condition"],
        port_signal=bulletin_fields["port_signal"],
        storm_surge_warning=bulletin_fields["storm_surge_warning"],
        coast_block=bulletin_fields["coast_block"],
        bulletin_provenance=bulletin_fields["bulletin_provenance"],
        bulletin_valid_from=bulletin_fields["bulletin_valid_from"],
        bulletin_valid_to=bulletin_fields["bulletin_valid_to"],
        handoff_provider=_make_handoff_provider(lat, lon),
    )

    try:
        outcome = engine.evaluate(ctx)
    except Exception as exc:
        return ToolResult(
            tool="evaluate_verdict",
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            summary=f"evaluate_verdict failed: {exc}",
            missing=["verdict_engine"],
        )

    verdict = outcome.verdict
    _OUTCOMES["__last__"] = outcome
    if evidence_query_id:
        _OUTCOMES[evidence_query_id] = outcome
    payload = outcome.to_dict()
    payload["handoff"] = verdict.handoff.to_dict() if verdict.handoff else None
    payload["evidence_count"] = len(observations)
    payload["sources_used"] = sources_used

    lines = [f"Verdict: {verdict.level}."]
    if verdict.reasons:
        lines.append(verdict.reasons[0])
    if verdict.ceiling_applied:
        rules = ", ".join(outcome.ceiling.rules_fired) or "advisory ceiling"
        lines.append(
            f"Downgraded from {verdict.downgraded_from} to {verdict.level} by the "
            f"advisory ceiling ({rules})."
        )
    if verdict.level == "DO_NOT_ADVISE" and verdict.handoff:
        lines.append(
            f"Handoff: {verdict.handoff.authority_name} "
            f"({verdict.handoff.authority_type}, contact "
            f"{verdict.handoff.contact or 'n/a'})."
        )
    summary = " ".join(lines)

    return ToolResult(
        tool="evaluate_verdict",
        ok=True,
        observations=observations + outcome.derived,
        payload=payload,
        summary=summary,
    )


__all__ = [
    "evaluate_verdict",
    "record_evidence",
    "evidence_for",
    "clear_evidence",
    "last_outcome",
]
