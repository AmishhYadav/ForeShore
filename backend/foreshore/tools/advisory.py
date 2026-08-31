"""Tool 1 — the governing advisory.

``get_governing_advisory`` fetches the IMD Coastal Weather Bulletin for the region's
configured office/coast block and parses its ``Sea Condition`` descriptor into a Douglas
sea-state band. This is the single most safety-relevant read in the whole tool layer:
every other tool may disagree with it, but nothing downstream may be more permissive
than what it says.

Two invariants enforced here, not just described:

* The Douglas band and its Hs range are *derived* from the bulletin's sea-condition
  string. Per CLAUDE.md invariant 3 ("no unsourced numbers"), that derived number is
  carried as its own :class:`~foreshore.models.Observation` with ``is_derived=True`` —
  it does not just appear in a summary string.
* A bulletin that cannot be read, cannot be matched to a coast block, cannot be parsed
  to a Douglas band, or cannot be dated is **never** treated as permissive. It comes
  back ``ok=True, partial=True`` with ``missing=["imd_coastal_bulletin"]`` so the caller
  (ultimately the verdict engine) abstains rather than guessing.
"""

from __future__ import annotations

from typing import Any

from ..models import Observation, Provenance, ToolResult, utcnow
from .registry import latlon_schema, registry


def _abstain(summary: str, *, observations: list[Observation] | None = None,
             payload: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult(
        tool="get_governing_advisory",
        ok=True,
        partial=True,
        observations=observations or [],
        payload=payload or {},
        missing=["imd_coastal_bulletin"],
        summary=summary,
    )


@registry.tool(
    name="get_governing_advisory",
    number=1,
    description=(
        "Fetch the governing IMD Coastal Weather Bulletin for this position and parse "
        "its Sea Condition descriptor into a Douglas sea-state band and significant "
        "wave height range. This bulletin is the advisory ceiling: FORESHORE may issue "
        "a more cautious verdict than it, never a more permissive one. Call this before "
        "evaluating any GO / GO_WITH_CAUTION / DO_NOT_ADVISE verdict — the ceiling "
        "cannot be applied without it. Also reports the bulletin's 12-hour validity "
        "window, whether it has expired, and whether a port signal is hoisted."
    ),
    schema=latlon_schema(),
    specialists=("RiskAssessment", "ReportingAgent"),
    reads_sources=("imd_coastal_bulletin",),
    cost="fast",
)
def get_governing_advisory(lat: float, lon: float) -> ToolResult:
    """Governing IMD bulletin + parsed Douglas band for ``(lat, lon)``.

    Never guesses a sea state and never substitutes a model wave height for the
    bulletin — an unreadable, undated, or unparseable bulletin is reported as a missing
    input, not filled in.
    """
    try:
        from ..sources.imd_bulletin import IMDCoastalBulletin, get_bulletin
        from ..verdict.ceiling import port_signal_is_nil
        from ..verdict.douglas import parse_sea_condition
    except Exception as exc:  # adapter or verdict package unavailable
        return _abstain(
            "The IMD Coastal Weather Bulletin adapter could not be loaded "
            f"({type(exc).__name__}: {exc}). No governing ceiling can be evaluated, so "
            "any advisory for this position must abstain (DO_NOT_ADVISE) until it is "
            "reachable."
        )

    try:
        source = IMDCoastalBulletin()
        bulletin = get_bulletin(region=source.region)
        # Re-parse through the adapter's own Source contract so every bulletin field
        # reaches the caller as a provenance-carrying Observation, not just a dataclass.
        raw = source.fetch()
        bulletin_observations = source.parse(raw)
    except Exception as exc:
        return _abstain(
            "Could not read the IMD Coastal Weather Bulletin "
            f"({type(exc).__name__}: {exc}). No sea state can be established for this "
            "position, so no verdict may be issued from it — abstain (DO_NOT_ADVISE)."
        )

    if bulletin.coast_block is None or bulletin.sea_condition is None:
        return _abstain(
            "The IMD bulletin page did not contain a Sea Condition entry for this "
            "region's configured coast block (or any fallback block). No ceiling can be "
            "evaluated from it; abstain (DO_NOT_ADVISE).",
            observations=bulletin_observations,
            payload={"bulletin": bulletin.to_dict()},
        )

    reading = parse_sea_condition(bulletin.sea_condition)
    if not reading.parsed or bulletin.valid_from is None or bulletin.valid_to is None:
        return _abstain(
            f"IMD sea condition {bulletin.sea_condition!r} for {bulletin.coast_block} "
            "could not be mapped to a Douglas band, or the bulletin's validity window "
            "could not be dated. An unparseable or undated ceiling cannot authorise a "
            "trip — abstain (DO_NOT_ADVISE).",
            observations=bulletin_observations,
            payload={"bulletin": bulletin.to_dict(), "sea_state": reading.to_dict()},
        )

    now = utcnow()
    expired = now > bulletin.valid_to
    port_nil = port_signal_is_nil(bulletin.port_signal)

    # The bulletin's own field observations already carry the shared Provenance for
    # this fetch (see sources/imd_bulletin.py); the Douglas band is *derived* from the
    # sea_condition field, so it gets its own derived Observation with the same
    # provenance lineage rather than appearing only in the summary.
    sea_condition_obs = next(
        (o for o in bulletin_observations if o.variable == "sea_condition"), None
    )
    bulletin_provenance = sea_condition_obs.provenance if sea_condition_obs else None

    douglas_observation: Observation | None = None
    if bulletin_provenance is not None:
        douglas_observation = Observation(
            variable="douglas_band",
            value=reading.band,
            unit="band",
            lat=lat,
            lon=lon,
            valid_time=bulletin.valid_from,
            provenance=Provenance(
                source_id="foreshore_derived_douglas",
                source_name=(
                    "FORESHORE derived Douglas band (parsed from IMD sea condition)"
                ),
                authority="derived",
                url=bulletin_provenance.url,
                acquired_at=utcnow(),
                issued_at=bulletin_provenance.issued_at,
                valid_from=bulletin_provenance.valid_from,
                valid_to=bulletin_provenance.valid_to,
                spatial_resolution_m=bulletin_provenance.spatial_resolution_m,
                is_derived=True,
                notes=(
                    f"Parsed from IMD sea condition {reading.raw!r} via "
                    "verdict.douglas.parse_sea_condition; worst band of all descriptors "
                    "present is taken, never averaged."
                ),
            ),
            qualifiers={
                "descriptor": reading.descriptor,
                "hs_low_m": reading.hs_low_m,
                "hs_high_m": reading.hs_high_m,
                "raw": reading.raw,
                "all_descriptors": list(reading.all_descriptors),
                "all_bands": list(reading.all_bands),
                "escalating": reading.escalating,
                "coast_block": bulletin.coast_block,
            },
        )

    observations = list(bulletin_observations)
    if douglas_observation is not None:
        observations.append(douglas_observation)

    payload: dict[str, Any] = {
        "bulletin": bulletin.to_dict(),
        "sea_state": reading.to_dict(),
        "douglas_band": reading.band,
        "hs_band_m": [reading.hs_low_m, reading.hs_high_m],
        "port_signal_nil": port_nil,
        "validity": {
            "from": bulletin.valid_from.isoformat(),
            "to": bulletin.valid_to.isoformat(),
            "hours": 12,
            "expired": expired,
        },
        "governs": (
            "This bulletin is the advisory ceiling. FORESHORE may be more cautious than "
            "it, never more permissive."
        ),
    }

    worst_note = ""
    if len(reading.all_bands) > 1:
        worst_note = (
            f" The bulletin names {len(reading.all_bands)} sea states "
            f"({', '.join(reading.all_descriptors)}); the worst is taken, never averaged."
        )

    summary = (
        f"{bulletin.coast_block} (IMD office {bulletin.office_name or bulletin.office_id}) — "
        f"Sea Condition: {bulletin.sea_condition!r}. Parsed to {reading.descriptor} "
        f"(Douglas {reading.band}, Hs {reading.hs_low_m:.2f}-{reading.hs_high_m:.2f} m)."
        f"{worst_note} Valid {bulletin.valid_from.isoformat()} to "
        f"{bulletin.valid_to.isoformat()} (12 h window) — "
        f"{'EXPIRED' if expired else 'current'}. Port signal: "
        f"{'NIL' if port_nil else (bulletin.port_signal or 'not stated')}. This bulletin "
        "governs the advisory ceiling; FORESHORE may be more cautious, never more "
        "permissive."
    )

    return ToolResult(
        tool="get_governing_advisory",
        ok=True,
        observations=observations,
        payload=payload,
        summary=summary,
    )


__all__ = ["get_governing_advisory"]
