"""Tool 2 — sea state, every wave source side by side, unreconciled.

This is the demo centrepiece. On any given day, Palk Bay's three wave readings —
IMD's human-forecaster ``Sea Condition`` descriptor, the INCOIS OSF 11 km assimilated
coastal nest, and Open-Meteo's ~28 km global cross-check — routinely disagree. FORESHORE
does not average them into a single number: it shows all three, names the source and
resolution of each, and states plainly which one governs what. Per CLAUDE.md:

* The INCOIS OSF coastal nest is the finest-resolution assimilated model on this coast —
  it governs the NUMBER.
* The IMD Coastal Weather Bulletin is the advisory ceiling — it governs the PERMISSION
  (see ``get_governing_advisory``, tool 1; this tool only reports its descriptor).
* Sources disagreeing is not a bug to fix here. Averaging them would hide the very
  uncertainty a fisherman needs to see.

Four sources, fetched independently and defensively — one outage must never take the
others down with it:

* INCOIS OSF ``wave`` product (SWH, SWELL, WP, SWP) — the authoritative model.
* INCOIS OSF ``mwh`` product (MAXW). Per ``docs/DECISIONS.md`` D2, this feed has been
  returning an all-NaN grid upstream since at least 2026-08-28 — an INCOIS outage, not a
  bug in this adapter. When it yields nothing, that is recorded as unavailable with the
  reason; max wave height is never back-filled from significant wave height.
* Open-Meteo Marine — coarse global cross-check, explicitly labelled as such.
* The IMD Coastal Bulletin's ``Sea Condition`` descriptor (a string, not a number).

Wave steepness (Hs / deep-water wavelength) is derived from the governing Hs and period
via ``verdict.engine.steepness`` and carried as its own ``is_derived=True`` Observation —
per CLAUDE.md invariant 3 ("no unsourced numbers"), a derived figure is never allowed to
appear only in a summary string. The same applies to the Douglas band implied by the IMD
descriptor: if a summary is going to say "(Douglas 5)", a Douglas Observation must exist
to source that "5" (see ``get_governing_advisory`` in ``tools/advisory.py`` — same
approach, reused here rather than reinvented).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from ..models import Observation, Provenance, ToolResult, utcnow
from ..verdict.douglas import parse_sea_condition
from ..verdict.engine import all_readings, describe_disagreements, governing, steepness
from .registry import latlon_schema, registry

#: The tool's own name of the "not averaged" rule, repeated verbatim in the evidence
#: panel payload so the renderer never has to hand-author it.
RESOLUTION_NOTE = (
    "The finest-resolution assimilated model (INCOIS OSF coastal nest, ~11 km) governs "
    "the NUMBER. The IMD Coastal Bulletin governs the PERMISSION. Sources are never "
    "averaged -- they are shown side by side."
)

#: Canonical variables requested from each wave source. Names match the adapters'
#: published canonical vocabulary exactly -- see incois_thredds.UNITS / openmeteo.MARINE_VARS.
_INCOIS_WAVE_VARIABLES: tuple[str, ...] = (
    "significant_wave_height", "swell_wave_height", "wave_period", "swell_wave_period",
)
_INCOIS_MWH_VARIABLES: tuple[str, ...] = ("max_wave_height",)
_OPENMETEO_VARIABLES: tuple[str, ...] = (
    "significant_wave_height", "swell_wave_height", "wind_wave_height",
    "wave_period", "swell_wave_period",
)

#: All four sources this tool reads. Used to seed the abstention path's ``missing`` list.
_ALL_SOURCES: tuple[str, ...] = (
    "incois_osf_wave", "incois_osf_mwh", "openmeteo_marine", "imd_coastal_bulletin",
)


def _parse_when(when: str | None) -> tuple[datetime | None, str | None]:
    """Tolerant ISO-8601 parse. Returns ``(None, note)`` on any failure instead of
    raising -- an unparseable ``when`` degrades to "now", it never fails the tool."""
    if when is None:
        return None, None
    s = when.strip()
    if not s:
        return None, None
    try:
        s2 = f"{s[:-1]}+00:00" if s.endswith("Z") else s
        return datetime.fromisoformat(s2), None
    except ValueError:
        return None, f"could not parse when={when!r}; used current time instead"


def _abstain(summary: str, *, payload: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult(
        tool="get_sea_state",
        ok=True,
        partial=True,
        observations=[],
        payload=payload or {},
        missing=list(_ALL_SOURCES),
        summary=summary,
    )


# -- per-source fetch, each independent and never raising ------------------------------


def _fetch_incois(
    product: str, variables: Sequence[str], lat: float, lon: float, at: datetime | None
) -> tuple[list[Observation], str | None]:
    """Best-effort INCOIS OSF ``product`` point query. Never raises: returns
    ``([], reason)`` on any failure so one product's outage cannot take the others down.
    """
    try:
        from ..sources.incois_thredds import IncoisThredds
    except Exception as exc:  # noqa: BLE001 - adapter module itself unavailable
        return [], f"INCOIS OSF adapter could not be loaded ({type(exc).__name__}: {exc})"
    try:
        obs = IncoisThredds().point(product, lat, lon, at=at, variables=list(variables))
    except Exception as exc:  # noqa: BLE001 - transport/parse failure must not fail the tool
        return [], f"INCOIS OSF {product!r} point query failed ({type(exc).__name__}: {exc})"
    if not obs:
        if product == "mwh":
            # Per docs/DECISIONS.md D2: MAXW has been all-NaN upstream since at least
            # 2026-08-28 -- an INCOIS outage, not a subsetting bug in this adapter.
            # Never back-fill max wave height from significant wave height.
            return [], (
                "INCOIS OSF max-wave-height (MAXW) is returning an all-NaN grid upstream "
                "(docs/DECISIONS.md D2); max wave height is never back-filled from Hs."
            )
        return [], f"INCOIS OSF {product!r} nest returned no data for this position and time"
    return obs, None


def _fetch_openmeteo_marine(
    lat: float, lon: float, when: datetime | None, variables: Sequence[str]
) -> tuple[list[Observation], str | None]:
    """Best-effort Open-Meteo Marine point query. Never raises."""
    try:
        from ..sources.openmeteo import OpenMeteoMarine
    except Exception as exc:  # noqa: BLE001 - adapter module itself unavailable
        return [], f"Open-Meteo Marine adapter could not be loaded ({type(exc).__name__}: {exc})"
    try:
        obs = OpenMeteoMarine().at(lat=lat, lon=lon, when=when, variables=list(variables))
    except Exception as exc:  # noqa: BLE001 - transport/parse failure must not fail the tool
        return [], f"Open-Meteo Marine query failed ({type(exc).__name__}: {exc})"
    if not obs:
        return [], "Open-Meteo Marine returned no data for this position and time"
    return obs, None


def _fetch_bulletin_sea_condition() -> tuple[Observation | None, str | None, dict | None, str | None]:
    """Best-effort IMD bulletin ``Sea Condition``. Never raises.

    Follows the exact call pattern ``tools/advisory.py`` uses: a fresh source instance's
    own ``.region`` resolves the office/coast block, and ``.fetch()`` + ``.parse()`` are
    re-run so the descriptor reaches the caller as a provenance-carrying Observation, not
    just a ``CoastalBulletin`` dataclass field.

    Returns ``(sea_condition_observation, sea_condition_text, bulletin_dict, reason)``.
    """
    try:
        from ..sources.imd_bulletin import IMDCoastalBulletin, get_bulletin
    except Exception as exc:  # noqa: BLE001 - adapter module itself unavailable
        return None, None, None, f"IMD bulletin adapter could not be loaded ({type(exc).__name__}: {exc})"
    try:
        source = IMDCoastalBulletin()
        bulletin = get_bulletin(region=source.region)
        raw = source.fetch()
        bulletin_observations = source.parse(raw)
    except Exception as exc:  # noqa: BLE001 - transport/parse failure must not fail the tool
        return None, None, None, f"IMD Coastal Weather Bulletin could not be read ({type(exc).__name__}: {exc})"
    if bulletin.sea_condition is None:
        return None, None, bulletin.to_dict(), (
            "IMD bulletin page had no Sea Condition entry for this region's configured "
            "coast block"
        )
    sea_condition_obs = next(
        (o for o in bulletin_observations if o.variable == "sea_condition"), None
    )
    return sea_condition_obs, bulletin.sea_condition, bulletin.to_dict(), None


def _build_summary(
    observations: list[Observation],
    sea_condition: str | None,
    missing: list[str],
    parse_note: str | None,
) -> str:
    """One line naming each source and its own value separately -- built ONLY from
    observations actually retrieved. Never a blended number."""
    hs_readings = all_readings(observations, "significant_wave_height")
    by_source_hs = {o.provenance.source_id: o for o in hs_readings}

    parts: list[str] = []

    incois_id = next((sid for sid in by_source_hs if sid.startswith("incois_osf")), None)
    if incois_id is not None:
        o = by_source_hs[incois_id]
        res_km = (o.provenance.spatial_resolution_m or 0.0) / 1000.0
        parts.append(f"INCOIS OSF {o.numeric:.3f} m ({res_km:.0f} km, assimilated)")

    om = by_source_hs.get("openmeteo_marine")
    if om is not None:
        res_km = (om.provenance.spatial_resolution_m or 0.0) / 1000.0
        parts.append(f"Open-Meteo {om.numeric:.2f} m ({res_km:.0f} km global)")

    if sea_condition:
        reading = parse_sea_condition(sea_condition)
        band_note = f" (Douglas {reading.band})" if reading.parsed else ""
        parts.append(f"IMD bulletin {sea_condition!r}{band_note}")

    if not parts:
        summary = "No wave source returned a value for this position and time."
    else:
        summary = (
            " vs ".join(parts)
            + ". Not averaged; INCOIS governs the number, IMD governs the permission."
        )
    if missing:
        summary += f" Unavailable: {', '.join(missing)}."
    if parse_note:
        summary += f" ({parse_note})"
    return summary


@registry.tool(
    name="get_sea_state",
    number=2,
    description=(
        "Point sea state from EVERY available wave source, side by side and "
        "unreconciled, each with its own provenance and spatial resolution: the INCOIS "
        "OSF coastal nest (significant wave height, swell height, wave period, swell "
        "period -- ~11 km, assimilated), the INCOIS OSF max-wave-height product, "
        "Open-Meteo Marine as a coarse ~28 km global cross-check, and the IMD Coastal "
        "Bulletin's Sea Condition descriptor. The INCOIS OSF coastal nest governs the "
        "NUMBER; the IMD Coastal Bulletin governs the PERMISSION. Sources are never "
        "averaged -- this is the disagreement panel."
    ),
    schema=latlon_schema(
        when={"type": ["string", "null"], "description": "ISO-8601 timestamp; omit or null for now."}
    ),
    specialists=("OceanAnalytics",),
    reads_sources=("incois_osf_wave", "incois_osf_mwh", "openmeteo_marine", "imd_coastal_bulletin"),
    cost="slow",
)
def get_sea_state(lat: float, lon: float, when: str | None = None) -> ToolResult:
    """Sea state at ``(lat, lon)`` and ``when`` (default: now) from every wave source.

    Never raises. Each of the four sources is fetched independently; one failing never
    fails the others or the tool. If literally no source returns anything, this abstains
    with ``ok=True, partial=True`` rather than guessing. INCOIS OSF max-wave-height is a
    known-degraded feed (docs/DECISIONS.md D2) and is reported as unavailable, never
    back-filled from significant wave height.
    """
    when_dt, parse_note = _parse_when(when)

    observations: list[Observation] = []
    unavailable: dict[str, str] = {}
    missing: list[str] = []

    wave_obs, wave_reason = _fetch_incois("wave", _INCOIS_WAVE_VARIABLES, lat, lon, when_dt)
    if wave_reason is not None:
        unavailable["incois_osf_wave"] = wave_reason
        missing.append("incois_osf_wave")
    observations.extend(wave_obs)

    mwh_obs, mwh_reason = _fetch_incois("mwh", _INCOIS_MWH_VARIABLES, lat, lon, when_dt)
    if mwh_reason is not None:
        unavailable["incois_osf_mwh"] = mwh_reason
        missing.append("incois_osf_mwh")
    observations.extend(mwh_obs)

    om_obs, om_reason = _fetch_openmeteo_marine(lat, lon, when_dt, _OPENMETEO_VARIABLES)
    if om_reason is not None:
        unavailable["openmeteo_marine"] = om_reason
        missing.append("openmeteo_marine")
    observations.extend(om_obs)

    sea_condition_obs, sea_condition, bulletin_dict, bulletin_reason = _fetch_bulletin_sea_condition()
    if bulletin_reason is not None:
        unavailable["imd_coastal_bulletin"] = bulletin_reason
        missing.append("imd_coastal_bulletin")
    elif sea_condition_obs is not None:
        observations.append(sea_condition_obs)

    if not observations:
        return _abstain(
            "No wave source (INCOIS OSF, Open-Meteo Marine, or the IMD Coastal Bulletin) "
            "returned anything for this position and time. No sea state can be reported; "
            "abstain rather than guess.",
            payload={"unavailable": unavailable, "resolution_note": RESOLUTION_NOTE},
        )

    # -- derived: wave steepness, from the governing Hs and period ---------------------
    # Reused verbatim from verdict.engine -- never reimplemented here. Height alone does
    # not capsize a small boat; steep short-period sea does, and no source publishes it.
    hs = governing(observations, "significant_wave_height")
    period = governing(observations, "wave_period")
    steep = steepness(hs.numeric if hs else None, period.numeric if period else None)
    if steep is not None and hs is not None and period is not None:
        observations.append(Observation(
            variable="wave_steepness",
            value=round(steep, 5),
            unit="ratio",
            lat=lat,
            lon=lon,
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
                    f"Derived from {hs.provenance.source_id} Hs and "
                    f"{period.provenance.source_id} period; steep short-period sea is the "
                    "small-boat capsize mode and no source publishes it directly."
                ),
            ),
            qualifiers={
                "hs_m": hs.numeric,
                "period_s": period.numeric,
                "inputs": [hs.provenance.provenance_id, period.provenance.provenance_id],
            },
        ))

    # -- derived: Douglas band implied by the IMD descriptor ----------------------------
    # Same approach as tools/advisory.py's get_governing_advisory: a summary line is
    # about to say "(Douglas N)", so that N must be a sourced Observation, not a bare
    # number conjured in a format string.
    if sea_condition and sea_condition_obs is not None:
        reading = parse_sea_condition(sea_condition)
        if reading.parsed:
            bp = sea_condition_obs.provenance
            observations.append(Observation(
                variable="douglas_band",
                value=reading.band,
                unit="band",
                lat=lat,
                lon=lon,
                valid_time=bp.valid_from or bp.issued_at or bp.acquired_at,
                provenance=Provenance(
                    source_id="foreshore_derived_douglas",
                    source_name="FORESHORE derived Douglas band (parsed from IMD sea condition)",
                    authority="derived",
                    url=bp.url,
                    acquired_at=utcnow(),
                    issued_at=bp.issued_at,
                    valid_from=bp.valid_from,
                    valid_to=bp.valid_to,
                    spatial_resolution_m=bp.spatial_resolution_m,
                    is_derived=True,
                    notes=(
                        f"Parsed from IMD sea condition {reading.raw!r} via "
                        "verdict.douglas.parse_sea_condition; worst band of all "
                        "descriptors present is taken, never averaged."
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
                },
            ))

    # -- payload -------------------------------------------------------------------------
    governing_payload: dict[str, dict[str, Any]] = {}
    for variable in ("significant_wave_height", "swell_wave_height"):
        g = governing(observations, variable)
        if g is not None:
            governing_payload[variable] = {
                "source_id": g.provenance.source_id,
                "source_name": g.provenance.source_name,
                "value": g.numeric,
                "unit": g.unit,
                "resolution_m": g.provenance.spatial_resolution_m,
                "valid_time": g.valid_time.isoformat(),
            }

    readings_by_source: dict[str, list[dict[str, Any]]] = {}
    for o in observations:
        readings_by_source.setdefault(o.provenance.source_id, []).append(o.to_dict())

    payload: dict[str, Any] = {
        "disagreements": describe_disagreements(observations, sea_condition),
        "governing": governing_payload,
        "readings_by_source": readings_by_source,
        "unavailable": unavailable,
        "resolution_note": RESOLUTION_NOTE,
    }
    if bulletin_dict is not None:
        payload["bulletin"] = bulletin_dict
    if parse_note:
        payload["when_parse_note"] = parse_note

    return ToolResult(
        tool="get_sea_state",
        ok=True,
        observations=observations,
        payload=payload,
        summary=_build_summary(observations, sea_condition, missing, parse_note),
        partial=bool(missing),
        missing=missing,
    )


__all__ = ["get_sea_state"]
