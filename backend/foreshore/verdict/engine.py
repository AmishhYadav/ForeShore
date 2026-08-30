"""Verdict assembly.

The decision is computed **deterministically** from vessel thresholds and sourced
observations. The LLM's role is to select tools, explain, and translate — it may make a
verdict *more* cautious, never less, and it cannot supply a value. Then the advisory
ceiling runs last, over the top of everything.

That ordering is the whole safety argument, so it is worth stating plainly:

    deterministic baseline  ->  (optional) LLM may only downgrade  ->  advisory ceiling

A consequence worth keeping: with no ``ANTHROPIC_API_KEY`` at all, this module still
produces a correct, fully-sourced verdict. The LLM is a presentation layer over a
decision it did not make.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Sequence

from ..config import RegionConfig, VesselClass, load_region, load_vessels
from ..models import (
    Handoff,
    Observation,
    Provenance,
    Verdict,
    VerdictLevel,
    is_more_permissive,
    utcnow,
    worst_verdict,
)
from .ceiling import CeilingInput, CeilingResult, apply_ceiling, compute_ceiling, regional_handoff
from .douglas import band_for_hs, bands_disagree, descriptor_for_hs, parse_sea_condition

#: Which source_id governs which variable when several disagree. INCOIS OSF is the
#: authoritative wave model for this coast (11 km, assimilated); Open-Meteo is a coarse
#: global cross-check. Sources are never averaged — the governing one is named.
GOVERNING_SOURCE_ORDER: dict[str, tuple[str, ...]] = {
    "significant_wave_height": ("incois_osf_wave", "incois_osf", "openmeteo_marine"),
    "swell_wave_height": ("incois_osf_wave", "incois_osf", "openmeteo_marine"),
    "wave_period": ("incois_osf_wave", "incois_osf", "openmeteo_marine"),
    "swell_wave_period": ("incois_osf_wave", "incois_osf", "openmeteo_marine"),
    "max_wave_height": ("incois_osf_mwh", "incois_osf"),
    "wind_speed": ("imd_geoserver", "openmeteo_forecast", "incois_osf_winds"),
    "wind_gust": ("openmeteo_forecast",),
    "current_speed": ("incois_osf_currents", "openmeteo_marine"),
    "visibility": ("openmeteo_forecast",),
}


def _rank(obs: Observation, order: Sequence[str]) -> int:
    sid = obs.provenance.source_id
    for i, prefix in enumerate(order):
        if sid == prefix or sid.startswith(prefix):
            return i
    return len(order)


def governing(observations: Iterable[Observation], variable: str) -> Observation | None:
    """The reading that governs for a variable, and the reason it does.

    Returns the highest-ranked source's value. It does **not** blend, and the losers stay
    in the evidence list so the panel can show the disagreement side by side.
    """
    order = GOVERNING_SOURCE_ORDER.get(variable, ())
    candidates = [o for o in observations if o.variable == variable and o.is_numeric]
    if not candidates:
        return None
    candidates.sort(key=lambda o: (_rank(o, order), abs((utcnow() - o.valid_time).total_seconds())))
    return candidates[0]


def all_readings(observations: Iterable[Observation], variable: str) -> list[Observation]:
    return [o for o in observations if o.variable == variable]


def steepness(hs_m: float | None, period_s: float | None) -> float | None:
    """Wave steepness Hs / L, deep-water L = 1.56 T^2.

    Height alone does not capsize a small boat; steep short-period sea does. A 1.5 m sea
    at 4 s is more dangerous to a vallam than 2.5 m at 12 s, and no source publishes this
    directly, so it is derived here and carried as a derived observation.
    """
    if not hs_m or not period_s or period_s <= 0:
        return None
    wavelength = 1.56 * period_s * period_s
    return hs_m / wavelength if wavelength > 0 else None


@dataclass
class Threshold:
    """One deterministic check against a vessel limit."""

    variable: str
    value: float | None
    unit: str
    go_limit: float | None
    caution_limit: float | None
    level: VerdictLevel
    reason: str
    observation: Observation | None = None
    higher_is_worse: bool = True

    def to_dict(self) -> dict:
        return {
            "variable": self.variable,
            "value": self.value,
            "unit": self.unit,
            "go_limit": self.go_limit,
            "caution_limit": self.caution_limit,
            "level": self.level,
            "reason": self.reason,
        }


@dataclass
class VerdictContext:
    """Everything the engine needs, all of it already sourced."""

    lat: float
    lon: float
    observations: list[Observation]
    vessel_class_id: str | None = None
    district: str | None = None
    when: datetime | None = None
    #: Bulletin fields, passed through from sources.imd_bulletin.
    sea_condition: str | None = None
    port_signal: str | None = None
    storm_surge_warning: str | None = None
    coast_block: str | None = None
    bulletin_provenance: Provenance | None = None
    bulletin_valid_from: datetime | None = None
    bulletin_valid_to: datetime | None = None
    #: Called only when the verdict abstains, so the handoff names a real landing centre.
    handoff_provider: Callable[[], Handoff | None] | None = None
    #: A level the LLM proposed. It may only make the verdict worse.
    llm_proposed: VerdictLevel | None = None
    llm_reasons: list[str] = field(default_factory=list)


@dataclass
class VerdictOutcome:
    verdict: Verdict
    thresholds: list[Threshold]
    ceiling: CeilingResult
    disagreements: list[dict]
    derived: list[Observation]

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.to_dict(),
            "thresholds": [t.to_dict() for t in self.thresholds],
            "ceiling": self.ceiling.to_dict(),
            "disagreements": self.disagreements,
        }


def _check(
    variable: str,
    obs: Observation | None,
    go_limit: float | None,
    caution_limit: float | None,
    unit: str,
    label: str,
) -> Threshold:
    value = obs.numeric if obs else None
    if value is None or go_limit is None or caution_limit is None:
        return Threshold(
            variable, value, unit, go_limit, caution_limit, "GO",
            reason=f"{label}: no sourced value available.", observation=obs,
        )
    if value > caution_limit:
        level: VerdictLevel = "DO_NOT_ADVISE"
        reason = f"{label} {value:.2f} {unit} exceeds the {caution_limit:.2f} {unit} limit for this boat."
    elif value > go_limit:
        level = "GO_WITH_CAUTION"
        reason = f"{label} {value:.2f} {unit} is above the {go_limit:.2f} {unit} comfortable limit."
    else:
        level = "GO"
        reason = f"{label} {value:.2f} {unit} is within limits ({go_limit:.2f} {unit})."
    return Threshold(variable, value, unit, go_limit, caution_limit, level, reason, obs)


def evaluate(
    ctx: VerdictContext,
    *,
    region: RegionConfig | None = None,
    vessel: VesselClass | None = None,
) -> VerdictOutcome:
    """Compute the verdict. Deterministic, then LLM-may-worsen, then ceiling."""
    region = region or load_region()
    vessel = vessel or load_vessels().get(ctx.vessel_class_id)
    obs = list(ctx.observations)
    district = ctx.district or region.district_for(ctx.lat, ctx.lon)

    hs = governing(obs, "significant_wave_height")
    period = governing(obs, "wave_period")
    swell_period = governing(obs, "swell_wave_period")
    wind = governing(obs, "wind_speed")
    gust = governing(obs, "wind_gust")
    vis = governing(obs, "visibility")

    derived: list[Observation] = []
    steep = steepness(hs.numeric if hs else None, period.numeric if period else None)
    steep_obs: Observation | None = None
    if steep is not None and hs is not None and period is not None:
        steep_obs = Observation(
            variable="wave_steepness",
            value=round(steep, 5),
            unit="ratio",
            lat=ctx.lat,
            lon=ctx.lon,
            valid_time=hs.valid_time,
            provenance=Provenance(
                source_id="foreshore_derived_steepness",
                source_name="FORESHORE derived wave steepness (Hs / 1.56 T^2)",
                authority="derived",
                url="local://derived/steepness",
                acquired_at=utcnow(),
                issued_at=hs.provenance.issued_at,
                valid_from=hs.provenance.valid_from,
                valid_to=hs.provenance.valid_to,
                spatial_resolution_m=hs.provenance.spatial_resolution_m,
                is_derived=True,
                notes=(
                    "Derived from "
                    f"{hs.provenance.source_id} Hs and {period.provenance.source_id} period; "
                    "steep short-period sea is the small-boat capsize mode, and no source "
                    "publishes it directly."
                ),
            ),
            qualifiers={
                "hs_m": hs.numeric,
                "period_s": period.numeric,
                "inputs": [hs.provenance.provenance_id, period.provenance.provenance_id],
            },
        )
        derived.append(steep_obs)

    thresholds = [
        _check("significant_wave_height", hs, vessel.limit("hs_go_m"),
               vessel.limit("hs_caution_m"), "m", "Significant wave height"),
        _check("wind_speed", wind, vessel.limit("wind_go_kn"),
               vessel.limit("wind_caution_kn"), "kn", "Wind"),
        _check("wind_gust", gust, vessel.limit("wind_caution_kn"),
               vessel.limit("gust_caution_kn"), "kn", "Gusts"),
        _check("wave_steepness", steep_obs, vessel.limit("steepness_caution"),
               (vessel.limit("steepness_caution") or 0) * 1.4 or None, "ratio",
               "Wave steepness"),
    ]
    if vis is not None and vis.numeric is not None:
        limit = vessel.limit("visibility_caution_m")
        level: VerdictLevel = "GO"
        reason = f"Visibility {vis.numeric:.0f} m is adequate."
        if limit and vis.numeric < limit:
            level = "GO_WITH_CAUTION"
            reason = f"Visibility {vis.numeric:.0f} m is below the {limit:.0f} m limit."
        thresholds.append(
            Threshold("visibility", vis.numeric, "m", limit, None, level, reason, vis,
                      higher_is_worse=False)
        )

    baseline = worst_verdict([t.level for t in thresholds])
    reasons = [t.reason for t in thresholds if t.level != "GO"]
    if not reasons:
        reasons = [t.reason for t in thresholds if t.observation is not None][:2]

    # The LLM may only make it worse. This is enforced, not requested.
    level = baseline
    if ctx.llm_proposed is not None:
        if is_more_permissive(ctx.llm_proposed, baseline):
            reasons.append(
                f"The language model proposed {ctx.llm_proposed}; the deterministic "
                f"threshold check gives {baseline} and the more cautious of the two governs."
            )
        else:
            level = ctx.llm_proposed
            reasons.extend(ctx.llm_reasons)

    verdict = Verdict(
        level=level,
        reasons=reasons,
        evidence=obs + derived,
        vessel_class=vessel.class_id,
        lat=ctx.lat,
        lon=ctx.lon,
    )

    ci = CeilingInput(
        sea_condition=ctx.sea_condition,
        bulletin_provenance=ctx.bulletin_provenance,
        port_signal=ctx.port_signal,
        storm_surge_warning=ctx.storm_surge_warning,
        valid_from=ctx.bulletin_valid_from,
        valid_to=ctx.bulletin_valid_to,
        coast_block=ctx.coast_block,
        swell_period_s=(swell_period.numeric if swell_period else
                        (period.numeric if period else None)),
        district=district,
        vessel_class_id=vessel.class_id,
        now=ctx.when,
    )
    ceiling = compute_ceiling(ci, region=region, vessel=vessel)
    apply_ceiling(
        verdict, ci, region=region, vessel=vessel,
        handoff_provider=ctx.handoff_provider,
    )

    return VerdictOutcome(
        verdict=verdict,
        thresholds=thresholds,
        ceiling=ceiling,
        disagreements=describe_disagreements(obs, ctx.sea_condition),
        derived=derived,
    )


def describe_disagreements(
    observations: Sequence[Observation], sea_condition: str | None = None
) -> list[dict]:
    """Sources side by side, unreconciled, with the governing one named.

    This is the centrepiece of the demo. IMD's human bulletin, INCOIS's 11 km assimilated
    nest and Open-Meteo's coarse global model disagree about Palk Bay most days. Showing
    the disagreement and naming which one governs is judgment; averaging them would be
    the opposite.
    """
    out: list[dict] = []
    reading = parse_sea_condition(sea_condition)

    for variable in ("significant_wave_height", "wind_speed", "swell_wave_height"):
        readings = [o for o in observations if o.variable == variable and o.is_numeric]
        if len(readings) < 2 and not (variable == "significant_wave_height" and reading.parsed):
            continue
        gov = governing(observations, variable)
        rows = [
            {
                "source_id": o.provenance.source_id,
                "source_name": o.provenance.source_name,
                "authority": o.provenance.authority,
                "value": o.numeric,
                "unit": o.unit,
                "resolution_m": o.provenance.spatial_resolution_m,
                "freshness": o.provenance.freshness,
                "valid_time": o.valid_time.isoformat(),
                "governs_value": bool(
                    gov and o.provenance.provenance_id == gov.provenance.provenance_id
                ),
                "governs_permission": False,
                "douglas_band": (
                    band_for_hs(o.numeric) if variable == "significant_wave_height" else None
                ),
                "douglas_descriptor": (
                    descriptor_for_hs(o.numeric) if variable == "significant_wave_height" else None
                ),
            }
            for o in readings
        ]
        if variable == "significant_wave_height" and reading.parsed:
            rows.insert(
                0,
                {
                    "source_id": "imd_coastal_bulletin",
                    "source_name": "IMD Coastal Weather Bulletin (human forecaster)",
                    "authority": "IMD",
                    "value": None,
                    "unit": "descriptor",
                    "descriptor": reading.raw,
                    "resolution_m": None,
                    "freshness": None,
                    "valid_time": None,
                    "governs_value": False,
                    "governs_permission": True,
                    "governs_note": (
                        "IMD is the governing advisory: FORESHORE may be more cautious than "
                        "the bulletin, never more permissive. The finest-resolution "
                        "assimilated model still governs the number."
                    ),
                    "douglas_band": reading.band,
                    "douglas_descriptor": reading.descriptor,
                },
            )
        spread = [r["value"] for r in rows if isinstance(r.get("value"), (int, float))]
        out.append(
            {
                "variable": variable,
                "readings": rows,
                "spread": (round(max(spread) - min(spread), 3) if len(spread) > 1 else None),
                "disagrees_with_bulletin": (
                    bands_disagree(reading.band, gov.numeric if gov else None)
                    if variable == "significant_wave_height"
                    else None
                ),
                "resolution_note": (
                    "Not averaged. Two different things govern: the finest-resolution "
                    "assimilated model governs the NUMBER, the IMD bulletin governs the "
                    "PERMISSION."
                ),
            }
        )
    return out


__all__ = [
    "VerdictContext", "VerdictOutcome", "Threshold", "evaluate", "governing",
    "all_readings", "steepness", "describe_disagreements", "GOVERNING_SOURCE_ORDER",
]
