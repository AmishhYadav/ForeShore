"""Tide and current tools — sea-level window plus a two-source current cross-check.

Tool 5 (``get_tide``) reads the Open-Meteo marine sea-level series
(:class:`foreshore.sources.openmeteo.OpenMeteoMarine`). Open-Meteo is the only tide
source available keyless for this coast — there is no keyless INCOIS or IMD tide-table
feed — and every summary and payload field says so explicitly. The next high/next low
are never asserted from a tide table: they are the local maxima/minima the adapter
computes directly off the fetched series (see ``OpenMeteoMarine.tide_window``), so this
tool only reports what that computation found.

Tool 6 (``get_currents``) reads two sources independently and returns them side by
side, never averaged: the INCOIS OSF ``currents`` nest
(:class:`foreshore.sources.incois_thredds.IncoisThredds`), the authoritative model for
this coast, and Open-Meteo marine, a coarse global cross-check. Which one governs a
comparison is decided by :func:`foreshore.verdict.engine.governing` — the same ranking
the verdict engine itself uses — never by blending the two readings.

Both adapters (and the verdict engine's ``governing`` helper) are imported lazily inside
the tool functions so a half-written or temporarily failing module cannot prevent this
module from registering its tools with the process-wide registry.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import Observation, ToolResult, utcnow
from .registry import latlon_schema, registry

#: The two current sources this tool reads, and the order they are checked in. Kept as
#: constants so the payload/summary always uses the exact ids the verdict engine ranks.
_INCOIS_CURRENTS_SOURCE = "incois_osf_currents"
_OPENMETEO_MARINE_SOURCE = "openmeteo_marine"


def _parse_when(when: str | None) -> tuple[datetime | None, str | None]:
    """Tolerant ISO-8601 parse. Returns ``(None, note)`` on any failure instead of
    raising — an unparseable ``when`` degrades to "now", it never fails the tool."""
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


def _clamp_hours(hours: int | None) -> tuple[int, str | None]:
    """Defensive clamp to 1..240, default 24. Never raises on a bad LLM-supplied value —
    it degrades to the default (or the nearest bound) with a note instead."""
    if hours is None:
        return 24, None
    try:
        h = int(hours)
    except (TypeError, ValueError):
        return 24, f"could not parse hours={hours!r}; used the default 24 h window instead"
    if h < 1:
        return 1, f"hours={hours!r} clamped to the 1 h minimum"
    if h > 240:
        return 240, f"hours={hours!r} clamped to the 240 h maximum"
    return h, None


def _fmt(value: Any, decimals: int) -> str:
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------------------------
# Tool 5 — get_tide
# --------------------------------------------------------------------------------------


def _build_tide_summary(
    current: Observation, next_high: dict[str, Any] | None, next_low: dict[str, Any] | None
) -> str:
    """One line built ONLY from values actually present in the fetched series."""
    parts = [f"Sea level now {_fmt(current.value, 2)} {current.unit} MSL"]
    if next_high is not None:
        parts.append(
            f"next high {_fmt(next_high.get('value'), 2)} {next_high.get('unit', current.unit)} "
            f"at {next_high.get('time')}"
        )
    if next_low is not None:
        parts.append(
            f"next low {_fmt(next_low.get('value'), 2)} {next_low.get('unit', current.unit)} "
            f"at {next_low.get('time')}"
        )
    summary = "; ".join(parts) + "."
    summary += (
        " (Next high/low computed from the Open-Meteo marine series as local extrema, "
        "not from a tide table; Open-Meteo is the only keyless tide source for this coast.)"
    )
    return summary


@registry.tool(
    name="get_tide",
    number=5,
    description=(
        "Sea level height above MSL over a window, plus the next high and next low "
        "water COMPUTED from the series as local extrema -- never asserted from a tide "
        "table. Open-Meteo marine is the only tide source available keyless for this "
        "coast and is labelled as such throughout."
    ),
    schema=latlon_schema(
        hours={
            "type": ["integer", "null"],
            "description": "Window length in hours, default 24, max 240.",
        }
    ),
    specialists=("OceanAnalytics",),
    reads_sources=("openmeteo_marine",),
    cost="fast",
)
def get_tide(lat: float, lon: float, hours: int | None = None) -> ToolResult:
    """Sea-level series at ``(lat, lon)`` over ``hours`` (default 24, clamped 1..240),
    plus the next high/low computed as local extrema on that series.

    Never raises. An unparseable or out-of-range ``hours`` degrades to the default (or
    the nearest bound) with a note in both ``summary`` and ``payload``. No series at all
    (adapter unavailable, or empty response) returns ``ok=True, partial=True,
    missing=["openmeteo_marine"]`` with an abstaining summary — Open-Meteo is the only
    keyless tide source for this coast, so there is no fallback to try.
    """
    window_hours, hours_note = _clamp_hours(hours)

    try:
        from ..sources.openmeteo import OpenMeteoMarine
    except Exception as exc:  # noqa: BLE001 - adapter module itself unavailable
        return ToolResult(
            tool="get_tide",
            ok=True,
            partial=True,
            missing=["openmeteo_marine"],
            summary=(
                f"The Open-Meteo marine adapter could not be loaded ({type(exc).__name__}: "
                f"{exc}); no tide window can be computed. Open-Meteo is the only keyless "
                "tide source available for this coast, so this cannot fall back to another."
            ),
            payload={"hours": window_hours, "next_high": None, "next_low": None, "extrema": []},
        )

    try:
        adapter = OpenMeteoMarine()
        series, extras = adapter.tide_window(lat=lat, lon=lon, hours=window_hours)
    except Exception as exc:  # noqa: BLE001 - never let a transport error raise into the agent
        return ToolResult(
            tool="get_tide",
            ok=True,
            partial=True,
            missing=["openmeteo_marine"],
            summary=(
                f"get_tide could not reach Open-Meteo marine ({type(exc).__name__}: {exc}); "
                "no tide window can be computed. Open-Meteo is the only keyless tide source "
                "available for this coast."
            ),
            payload={"hours": window_hours, "next_high": None, "next_low": None, "extrema": []},
        )

    if not series:
        return ToolResult(
            tool="get_tide",
            ok=True,
            partial=True,
            missing=["openmeteo_marine"],
            summary=(
                "Open-Meteo returned no sea-level series for this position and window; "
                "tide cannot be assessed. Open-Meteo is the only keyless tide source "
                "available for this coast."
            ),
            payload={"hours": window_hours, "next_high": None, "next_low": None, "extrema": []},
        )

    now = utcnow()
    current = min(series, key=lambda o: abs((o.valid_time - now).total_seconds()))
    next_high = extras.get("next_high")
    next_low = extras.get("next_low")

    payload: dict[str, Any] = {
        "next_high": next_high,
        "next_low": next_low,
        "extrema": extras.get("extrema", []),
        "series": [o.to_dict() for o in series],
        "hours": window_hours,
        "note": (
            "next_high/next_low/extrema are COMPUTED from the Open-Meteo marine "
            "sea-level series as local maxima/minima, never asserted from a tide "
            "table; Open-Meteo is the only keyless tide source available for this coast."
        ),
    }
    if hours_note:
        payload["hours_note"] = hours_note

    summary = _build_tide_summary(current, next_high, next_low)
    if hours_note:
        summary += f" ({hours_note})"

    return ToolResult(
        tool="get_tide",
        ok=True,
        observations=series,
        payload=payload,
        summary=summary,
    )


# --------------------------------------------------------------------------------------
# Tool 6 — get_currents
# --------------------------------------------------------------------------------------


def _reading_for_source(observations: list[Observation]) -> dict[str, Any]:
    """One source's current_speed/current_direction, nested for the payload. Every
    number here also exists as one of the ``observations`` this tool returns."""
    if not observations:
        return {}
    by_var = {o.variable: o for o in observations}
    sample = observations[0]
    out: dict[str, Any] = {
        "source_name": sample.provenance.source_name,
        "authority": sample.provenance.authority,
        "valid_time": sample.valid_time.isoformat(),
        "resolution_m": sample.provenance.spatial_resolution_m,
        "freshness": sample.provenance.freshness,
    }
    speed = by_var.get("current_speed")
    direction = by_var.get("current_direction")
    if speed is not None:
        out["current_speed"] = {"value": speed.value, "unit": speed.unit}
    if direction is not None:
        out["current_direction"] = {"value": direction.value, "unit": direction.unit}
    return out


def _build_currents_summary(
    readings: dict[str, list[Observation]],
    unavailable: dict[str, str],
    governing_obs: Observation | None,
    when_note: str | None,
) -> str:
    """Each source named with its own value. Never an average of the two."""
    parts: list[str] = []
    for source_id, observations in readings.items():
        by_var = {o.variable: o for o in observations}
        speed = by_var.get("current_speed")
        direction = by_var.get("current_direction")
        label = observations[0].provenance.source_name if observations else source_id
        clause = label
        if speed is not None:
            clause += f": {_fmt(speed.value, 2)} {speed.unit}"
            if direction is not None:
                clause += f" from {_fmt(direction.value, 0)} deg"
        parts.append(clause)

    if parts:
        summary = "; ".join(parts) + ". Sources shown side by side, never averaged."
    else:
        summary = "No current data available from any source (INCOIS OSF or Open-Meteo marine)."

    if governing_obs is not None:
        summary += f" {governing_obs.provenance.source_name} governs the number for this coast."

    if unavailable:
        summary += " Unavailable: " + "; ".join(
            f"{sid} ({reason})" for sid, reason in unavailable.items()
        ) + "."

    if when_note:
        summary += f" ({when_note})"

    return summary


@registry.tool(
    name="get_currents",
    number=6,
    description=(
        "Surface current speed and direction from every available source side by side: "
        "the INCOIS OSF currents nest (the authoritative model for this coast) and "
        "Open-Meteo marine (a coarse global cross-check). Sources are never averaged -- "
        "the governing one is named and the other is kept in evidence regardless."
    ),
    schema=latlon_schema(
        when={
            "type": ["string", "null"],
            "description": "ISO-8601 timestamp; omit or null for now.",
        }
    ),
    specialists=("OceanAnalytics",),
    reads_sources=("incois_osf_currents", "openmeteo_marine"),
    cost="slow",
)
def get_currents(lat: float, lon: float, when: str | None = None) -> ToolResult:
    """Surface current speed/direction at ``(lat, lon)`` and ``when`` (default: now)
    from the INCOIS OSF currents nest and Open-Meteo marine, read independently.

    Never raises. Either source failing does not fail the other -- each is tried on its
    own and a failure is recorded in ``payload["unavailable"]`` rather than propagated.
    The governing reading is named via ``verdict.engine.governing`` (INCOIS OSF ranks
    first for ``current_speed``); the losing source stays in ``observations`` so nothing
    is hidden. If nothing is available from either source this still returns
    ``ok=True, partial=True`` with both named in ``missing`` -- a cross-check tool
    abstains rather than failing outright.
    """
    when_dt, when_note = _parse_when(when)

    readings: dict[str, list[Observation]] = {}
    unavailable: dict[str, str] = {}

    # -- INCOIS OSF currents: the authoritative model for this coast -------------------
    try:
        from ..sources.incois_thredds import IncoisThredds
    except Exception as exc:  # noqa: BLE001 - adapter module itself unavailable
        unavailable[_INCOIS_CURRENTS_SOURCE] = f"adapter unavailable: {type(exc).__name__}: {exc}"
    else:
        try:
            incois_obs = IncoisThredds().point("currents", lat, lon, at=when_dt)
        except Exception as exc:  # noqa: BLE001 - one source failing must not sink the tool
            unavailable[_INCOIS_CURRENTS_SOURCE] = f"{type(exc).__name__}: {exc}"
        else:
            if incois_obs:
                readings[_INCOIS_CURRENTS_SOURCE] = incois_obs
            else:
                unavailable[_INCOIS_CURRENTS_SOURCE] = (
                    "no current data returned for this position/time"
                )

    # -- Open-Meteo marine: coarse global cross-check, never blended with INCOIS -------
    try:
        from ..sources.openmeteo import OpenMeteoMarine
    except Exception as exc:  # noqa: BLE001 - adapter module itself unavailable
        unavailable[_OPENMETEO_MARINE_SOURCE] = f"adapter unavailable: {type(exc).__name__}: {exc}"
    else:
        try:
            om_obs = OpenMeteoMarine().at(
                lat=lat, lon=lon, when=when_dt, variables=["current_speed", "current_direction"]
            )
        except Exception as exc:  # noqa: BLE001
            unavailable[_OPENMETEO_MARINE_SOURCE] = f"{type(exc).__name__}: {exc}"
        else:
            if om_obs:
                readings[_OPENMETEO_MARINE_SOURCE] = om_obs
            else:
                unavailable[_OPENMETEO_MARINE_SOURCE] = (
                    "no current data returned for this position/time"
                )

    all_observations: list[Observation] = [o for obs in readings.values() for o in obs]

    governing_obs: Observation | None = None
    if all_observations:
        from ..verdict.engine import governing

        governing_obs = governing(all_observations, "current_speed")

    payload: dict[str, Any] = {
        "readings_by_source": {sid: _reading_for_source(obs) for sid, obs in readings.items()},
        "governing": governing_obs.to_dict() if governing_obs is not None else None,
        "unavailable": unavailable,
        "resolution_note": (
            "Not averaged: the INCOIS OSF coastal current nest governs the number for "
            "this coast; Open-Meteo marine is a coarse global cross-check shown "
            "alongside it, never blended with it."
        ),
    }
    if when_note:
        payload["when_note"] = when_note

    summary = _build_currents_summary(readings, unavailable, governing_obs, when_note)

    return ToolResult(
        tool="get_currents",
        ok=True,
        observations=all_observations,
        payload=payload,
        summary=summary,
        partial=bool(unavailable),
        missing=sorted(unavailable),
    )


__all__ = ["get_tide", "get_currents"]
