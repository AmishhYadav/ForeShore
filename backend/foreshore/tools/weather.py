"""Weather tools — point-in-time atmospheric forecast and IMD lightning nowcast.

Tool 3 (``get_weather``) reads the Open-Meteo ECMWF IFS cross-check forecast
(:class:`foreshore.sources.openmeteo.OpenMeteoForecast`). It is explicitly NOT an
authority on lightning: Open-Meteo's ``lightning_potential`` is null everywhere over
India (see that module's docstring), so this tool never requests or emits it, and its
CAPE observation always carries the "not a lightning probability" caveat in both the
qualifier and the human-readable summary.

Tool 4 (``get_lightning_nowcast``) is the system's only lightning authority: the IMD
district nowcast (:class:`foreshore.sources.imd_geoserver.IMDGeoServer`). When that
layer is unreachable or has no feature for the district, this tool abstains explicitly
rather than ever falling back to CAPE — a scored requirement, not a nicety.

Both adapters are imported lazily inside the tool functions so a half-written or
temporarily failing adapter module cannot prevent this module from registering its
tools with the process-wide registry.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import Observation, ToolResult, utcnow
from .registry import latlon_schema, registry

# Canonical variable names (as emitted by OpenMeteoForecast) that make up "weather" in
# this system's vocabulary. Deliberately excludes relative_humidity (not asked for by
# the contract) and never includes anything with "lightning" in the name.
_WEATHER_VARIABLES: tuple[str, ...] = (
    "wind_speed",
    "wind_gust",
    "wind_direction",
    "precipitation",
    "convective_available_potential_energy",
    "visibility",
    "air_temperature",
    "pressure_msl",
    "cloud_cover",
)


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


def _fmt(value: Any, decimals: int) -> str:
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


def _build_weather_summary(observations: list[Observation], parse_note: str | None) -> str:
    """One line built ONLY from observations actually present. CAPE, when present,
    always carries the "not a lightning probability" caveat."""
    by_var: dict[str, Observation] = {}
    for o in observations:
        by_var.setdefault(o.variable, o)

    parts: list[str] = []

    wind = by_var.get("wind_speed")
    gust = by_var.get("wind_gust")
    wdir = by_var.get("wind_direction")
    if wind is not None:
        clause = f"Wind {_fmt(wind.value, 1)} {wind.unit}"
        if gust is not None:
            clause += f" gusting {_fmt(gust.value, 1)} {gust.unit}"
        if wdir is not None:
            clause += f" from {_fmt(wdir.value, 0)} deg"
        parts.append(clause)
    elif gust is not None:
        parts.append(f"Gusts {_fmt(gust.value, 1)} {gust.unit}")

    vis = by_var.get("visibility")
    if vis is not None:
        parts.append(f"visibility {_fmt(vis.value, 0)} {vis.unit}")

    precip = by_var.get("precipitation")
    if precip is not None:
        parts.append(f"precipitation {_fmt(precip.value, 1)} {precip.unit}")

    temp = by_var.get("air_temperature")
    if temp is not None:
        parts.append(f"air temp {_fmt(temp.value, 1)} {temp.unit}")

    pressure = by_var.get("pressure_msl")
    if pressure is not None:
        parts.append(f"pressure {_fmt(pressure.value, 1)} {pressure.unit}")

    cloud = by_var.get("cloud_cover")
    if cloud is not None:
        parts.append(f"cloud cover {_fmt(cloud.value, 0)}{cloud.unit}")

    cape = by_var.get("convective_available_potential_energy")
    if cape is not None:
        parts.append(
            f"CAPE {_fmt(cape.value, 0)} {cape.unit} (CAPE is not a lightning probability)"
        )

    summary = (", ".join(parts) + ".") if parts else "No weather observations available."
    if parse_note:
        summary = f"{summary} ({parse_note})"
    return summary


@registry.tool(
    name="get_weather",
    number=3,
    description=(
        "Point-in-time atmospheric weather at a position: wind speed and gusts, wind "
        "direction, precipitation, CAPE, visibility, air temperature, pressure and cloud "
        "cover, from the Open-Meteo ECMWF IFS cross-check forecast. CAPE is NOT a "
        "lightning probability -- use get_lightning_nowcast for lightning risk."
    ),
    schema=latlon_schema(
        when={
            "type": ["string", "null"],
            "description": (
                "ISO-8601 timestamp to evaluate at, e.g. '2026-08-30T14:00:00Z'. "
                "Omit or null for now."
            ),
        }
    ),
    specialists=("WeatherIntelligence",),
    reads_sources=("openmeteo_forecast",),
    cost="fast",
)
def get_weather(lat: float, lon: float, when: str | None = None) -> ToolResult:
    """Fetch wind/gust/direction/precipitation/CAPE/visibility/temperature/pressure/
    cloud-cover at ``(lat, lon)`` and ``when`` (default: now) from Open-Meteo.

    Never raises. An unparseable ``when`` degrades to "now" with a note in ``summary``.
    A total adapter failure returns ``ok=False``; a partial one (some variables missing
    for the hour) returns ``ok=True, partial=True`` with the gaps in ``missing``.
    """
    when_dt, parse_note = _parse_when(when)

    try:
        from ..sources.openmeteo import OpenMeteoForecast
    except Exception as exc:  # noqa: BLE001 - adapter module itself unavailable
        return ToolResult(
            tool="get_weather",
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            summary="Weather adapter (Open-Meteo forecast) is unavailable.",
            missing=["openmeteo_forecast"],
        )

    try:
        adapter = OpenMeteoForecast()
        observations = adapter.at(
            lat=lat, lon=lon, when=when_dt, variables=list(_WEATHER_VARIABLES)
        )
    except Exception as exc:  # noqa: BLE001 - never let a transport error raise into the agent
        return ToolResult(
            tool="get_weather",
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            summary=f"get_weather failed: {exc}",
            missing=["openmeteo_forecast"],
        )

    if not observations:
        return ToolResult(
            tool="get_weather",
            ok=False,
            error="Open-Meteo returned no weather observations",
            summary=(
                "Open-Meteo returned no weather observations for this position and time."
            ),
            missing=["openmeteo_forecast"],
        )

    found = {o.variable for o in observations}
    missing_vars = [v for v in _WEATHER_VARIABLES if v not in found]
    by_variable = {
        o.variable: {
            "value": o.value,
            "unit": o.unit,
            "source_id": o.provenance.source_id,
        }
        for o in observations
    }

    return ToolResult(
        tool="get_weather",
        ok=True,
        observations=observations,
        payload={"by_variable": by_variable},
        summary=_build_weather_summary(observations, parse_note),
        partial=bool(missing_vars),
        missing=missing_vars,
    )


@registry.tool(
    name="get_lightning_nowcast",
    number=4,
    description=(
        "The system's only lightning authority: the IMD district-level nowcast/lightning "
        "warning. Give a district name, or a position to derive one, or neither to use "
        "the region's first anchor port. When IMD's nowcast layer is unreachable or has "
        "no feature for the district, this tool abstains explicitly rather than guessing "
        "-- CAPE from get_weather must never be substituted for it."
    ),
    schema={
        "type": "object",
        "properties": {
            "district": {
                "type": ["string", "null"],
                "description": "IMD district name, e.g. 'Ramanathapuram'. Omit to derive from lat/lon.",
            },
            "lat": {
                "type": ["number", "null"],
                "description": "Latitude, decimal degrees (EPSG:4326). Used to derive district if district is omitted.",
            },
            "lon": {
                "type": ["number", "null"],
                "description": "Longitude, decimal degrees (EPSG:4326). Used to derive district if district is omitted.",
            },
        },
        "required": [],
    },
    specialists=("WeatherIntelligence",),
    reads_sources=("imd_geoserver",),
    cost="fast",
)
def get_lightning_nowcast(
    district: str | None = None, lat: float | None = None, lon: float | None = None
) -> ToolResult:
    """IMD district nowcast (category/message + toi/vupto qualifiers) for ``district``,
    or one derived from ``(lat, lon)``, or the region's first anchor port if neither is
    given.

    Never raises and never falls back to CAPE. If the IMD nowcast layer cannot be
    reached, or has no feature for the resolved district, this returns an explicit
    abstention: ``ok=True, partial=True, missing=["imd_nowcast"]``, with
    ``payload["lightning_assessable"] = False``.
    """
    from ..config import load_region

    region = load_region()

    resolution_note: str | None = None
    resolved_district = district.strip() if isinstance(district, str) and district.strip() else None

    if resolved_district is None:
        if lat is not None and lon is not None:
            resolved_district = region.district_for(lat, lon)
            resolution_note = f"district derived from position ({lat}, {lon})"
            if resolved_district is None:
                fallback_port = region.anchor_ports[0]
                resolved_district = fallback_port.district
                resolution_note += f"; nearest port had no district on file, defaulted to {fallback_port.name}"
        else:
            port = region.anchor_ports[0]
            resolved_district = port.district
            resolution_note = f"no district or position given; defaulted to {port.name}"

    if not resolved_district:
        return ToolResult(
            tool="get_lightning_nowcast",
            ok=True,
            partial=True,
            missing=["imd_nowcast"],
            summary=(
                "Could not determine a district to check against the IMD nowcast; "
                "lightning risk cannot be assessed. CAPE is not a lightning probability "
                "and must not be substituted for the IMD nowcast."
            ),
            payload={"district": None, "warnings": [], "lightning_assessable": False},
        )

    try:
        from ..sources.imd_geoserver import IMDGeoServer
    except Exception as exc:  # noqa: BLE001 - adapter module itself unavailable
        return ToolResult(
            tool="get_lightning_nowcast",
            ok=True,
            partial=True,
            missing=["imd_nowcast"],
            summary=(
                f"IMD nowcast adapter is unavailable ({type(exc).__name__}: {exc}); "
                f"lightning risk for {resolved_district} cannot be assessed. CAPE must "
                "not be substituted for the IMD nowcast."
            ),
            payload={"district": resolved_district, "warnings": [], "lightning_assessable": False},
        )

    try:
        adapter = IMDGeoServer(region=region)
        observations = adapter.parse_nowcast(district=resolved_district)
    except Exception as exc:  # noqa: BLE001 - transport/parse failure must abstain, not raise
        return ToolResult(
            tool="get_lightning_nowcast",
            ok=True,
            partial=True,
            missing=["imd_nowcast"],
            summary=(
                f"IMD nowcast is unavailable right now ({type(exc).__name__}: {exc}) for "
                f"{resolved_district}; lightning cannot be assessed. CAPE is not a "
                "lightning probability and must not be substituted."
            ),
            payload={"district": resolved_district, "warnings": [], "lightning_assessable": False},
        )

    if not observations:
        return ToolResult(
            tool="get_lightning_nowcast",
            ok=True,
            partial=True,
            missing=["imd_nowcast"],
            summary=(
                f"IMD's nowcast layer has no feature for {resolved_district}; lightning "
                "cannot be assessed. CAPE is not a lightning probability and must not be "
                "substituted for the IMD nowcast."
            ),
            payload={"district": resolved_district, "warnings": [], "lightning_assessable": False},
        )

    def _describe(value: object) -> str:
        s = str(value).strip()
        return "no active nowcast warning (NIL)" if s.upper() == "NIL" else s

    labels = sorted({_describe(o.value) for o in observations})
    summary = f"IMD nowcast for {resolved_district}: {'; '.join(labels)}."
    timed = next(
        (o for o in observations if o.qualifiers.get("toi") or o.qualifiers.get("vupto")), None
    )
    if timed is not None:
        toi = timed.qualifiers.get("toi") or "?"
        vupto = timed.qualifiers.get("vupto") or "?"
        summary += f" Issued {toi} IST, valid until {vupto} IST."
    if resolution_note:
        summary += f" ({resolution_note})"

    return ToolResult(
        tool="get_lightning_nowcast",
        ok=True,
        observations=observations,
        payload={
            "district": resolved_district,
            "warnings": [o.to_dict() for o in observations],
            "lightning_assessable": True,
        },
        summary=summary,
    )


__all__ = ["get_weather", "get_lightning_nowcast"]
