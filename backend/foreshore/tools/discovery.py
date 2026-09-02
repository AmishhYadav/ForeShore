"""Tool 16 — ``list_available_data``, the sole tool of the ``MarineDataDiscovery``
specialist.

That specialist's own framing (``agents/specialists.py``): *"Find out what data
actually exists for this place and time, and how good it is."* / *"Report coverage
honestly: source, authority, spatial resolution, update cadence, and how old the
newest granule is. A gap is a finding, not a failure — name it."* This module is the
literal implementation of that mandate — it never fetches marine data itself, it
reports on the *health and shape* of the nine source adapters that do.

Real source count — nine, not eight
------------------------------------
``backend/foreshore/sources/`` holds eight adapter *modules*, and
``scripts/healthcheck.py`` reports eight *rows* — but that script folds
``OpenMeteoMarine`` and ``OpenMeteoForecast`` (two distinct :class:`~.base.Source`
subclasses living in ``openmeteo.py``, with different ``source_id``,
``spatial_resolution_m`` and ``validity``) into one combined "openmeteo" report line,
and does not have its own row for ``MarineRegionsIMBL`` folded into anything — it is
already a full, separate ``CHECKS`` entry there. A coverage table's whole job is to
show one row per real, distinct ``source_id`` a caller could otherwise call
``.health()`` on directly; inventing a synthetic combined "openmeteo" identifier here
would itself be an unsourced label. So this module probes all **nine** real adapters
individually: ``imd_coastal_bulletin``, ``imd_geoserver``, ``incois_wfs``,
``incois_osf``, ``incois_argo``, ``openmeteo_marine``, ``openmeteo_forecast``,
``gdacs_tc``, ``marine_regions_imbl``.

Mode discipline
----------------
Unlike ``scripts/healthcheck.py``, this module never sets ``FORESHORE_MODE`` — it is a
normal tool called during normal agent turns and must behave identically in
``live`` and ``fixture`` mode, replaying whatever :func:`foreshore.config.is_fixture`
already says. Each adapter's own :meth:`~.base.Source.health` already respects that
(via ``Source.get``), so nothing here needs to check the mode directly.

Isolation
---------
Every adapter is instantiated and probed independently, inside its own
try/except, mirroring ``scripts/healthcheck.py``'s ``_check`` — one source being
unreachable, mid-init, or (in tests) monkeypatched to raise, must never take the
other eight rows down with it. Most of the nine adapters' own ``.health()``
overrides already swallow their internal failures and return ``ok=False`` rows
(that is the existing, tested contract of ``Source.health``); the try/except here
exists for the layer *above* that — a raise straight out of ``.health()`` itself
(construction failure, or a monkeypatched override in a test) — the same class of
failure ``scripts/healthcheck.py`` isolates.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from ..config import RegionConfig, load_region
from ..models import UTC, Observation, Provenance, ToolResult, utcnow
from .registry import registry

log = logging.getLogger("foreshore.tools.discovery")


# ----------------------------------------------------------------------------------
# Per-adapter builders — one per real source_id, lazily importing inside the function
# body (never at module scope) so one adapter's import error cannot take the whole
# tools package down at ``tools/__init__.py`` import time. Mirrors
# ``scripts/healthcheck.py``'s own per-source lazy-import discipline.
# ----------------------------------------------------------------------------------


def _build_imd_bulletin(region: RegionConfig) -> Any:
    from ..sources.imd_bulletin import IMDCoastalBulletin

    return IMDCoastalBulletin(region=region)


def _build_imd_geoserver(region: RegionConfig) -> Any:
    from ..sources.imd_geoserver import IMDGeoServer

    return IMDGeoServer(region=region)


def _build_incois_wfs(region: RegionConfig) -> Any:
    from ..sources.incois_wfs import IncoisWFS

    return IncoisWFS(region=region)


def _build_incois_osf(region: RegionConfig) -> Any:
    from ..sources.incois_thredds import IncoisThredds

    return IncoisThredds(region=region)


def _build_incois_argo(region: RegionConfig) -> Any:
    from ..sources.incois_erddap import IncoisArgo

    return IncoisArgo(region=region)


def _build_openmeteo_marine(region: RegionConfig) -> Any:
    from ..sources.openmeteo import OpenMeteoMarine

    return OpenMeteoMarine(region=region)


def _build_openmeteo_forecast(region: RegionConfig) -> Any:
    from ..sources.openmeteo import OpenMeteoForecast

    return OpenMeteoForecast(region=region)


def _build_gdacs(region: RegionConfig) -> Any:
    from ..sources.gdacs import GDACSCyclones

    return GDACSCyclones(region=region)


def _build_marine_regions(region: RegionConfig) -> Any:
    from ..sources.marine_regions import MarineRegionsIMBL

    return MarineRegionsIMBL(region=region)


# ----------------------------------------------------------------------------------
# Fallback URLs — used only when an adapter instance's own ``base_url``/``url`` class
# attribute is unset (several adapters build their request URL from a module-level
# constant inside a method body rather than exposing it as ``self.base_url``). Every
# value below is the adapter module's own public constant, imported directly rather
# than retyped, so it cannot drift from the real endpoint.
# ----------------------------------------------------------------------------------


def _fallback_url(source_id: str) -> str:
    if source_id == "incois_wfs":
        from ..sources.incois_wfs import INCOIS_GEOSERVER

        return INCOIS_GEOSERVER
    if source_id == "incois_osf":
        from ..sources.incois_thredds import THREDDS

        return THREDDS
    if source_id == "incois_argo":
        from ..sources.incois_erddap import ERDDAP

        return ERDDAP
    if source_id == "gdacs_tc":
        from ..sources.gdacs import GDACS_EVENTLIST

        return GDACS_EVENTLIST
    if source_id == "marine_regions_imbl":
        from ..sources.marine_regions import VLIZ_WFS

        return VLIZ_WFS
    return ""


# ----------------------------------------------------------------------------------
# Update-cadence copy — one short, human-facing sentence per adapter. Every claim here
# traces to either CLAUDE.md's "Data sources — verified live" table or the adapter's
# own docstring / ``validity`` attribute (never an invented frequency).
# ----------------------------------------------------------------------------------

_CADENCE: dict[str, str] = {
    "imd_coastal_bulletin": (
        "IMD Coastal Bulletin, re-issued periodically by the coastal office; 12 h "
        "validity window — expires past 12 h per CLAUDE.md invariant 4."
    ),
    "imd_geoserver": (
        "District nowcast/lightning warnings, AWS station observations and cyclone "
        "track — each carries its own issue/valid-until time; adapter validity "
        "window 3 h."
    ),
    "incois_wfs": (
        "Official PFZ advisory lines carry Year/Julian_day (daily issuance); PFZ "
        "sectors, landing centres, eco-sensitive zones, HAB sectors and bathymetry "
        "are near-static reference layers; adapter validity window 24 h."
    ),
    "incois_osf": (
        "INCOIS Ocean State Forecast coastal nest: 56 steps at 3 h resolution over "
        "a 7-day forecast; ~2-day publication lag from model run to availability "
        "(CLAUDE.md 'INCOIS OSF coastal wave nest' table)."
    ),
    "incois_argo": (
        "10-day gridded objective analysis (incois_argo_10d_VAM); adapter validity "
        "window 15 days."
    ),
    "openmeteo_marine": (
        "Hourly global wave/tide/current forecast series (ECMWF-driven, ~28 km "
        "grid); cross-check only, not authoritative — INCOIS OSF governs; adapter "
        "validity window 6 h."
    ),
    "openmeteo_forecast": (
        "Hourly atmospheric forecast (ECMWF IFS best_match, ~11 km grid); "
        "cross-check only, not authoritative; adapter validity window 6 h."
    ),
    "gdacs_tc": (
        "Event-driven tropical-cyclone alert feed — 0 features means no active "
        "cyclone, a valid state, not an error (CLAUDE.md); adapter validity window "
        "6 h."
    ),
    "marine_regions_imbl": (
        "Static treaty maritime-boundary segments — 'a treaty boundary does not go "
        "stale' (adapter comment); adapter validity window 365 days, i.e. reference "
        "data, not a live feed."
    ),
}

#: (builder, expected source_id) — the nine adapters this tool probes, in the order
#: CLAUDE.md's own source table roughly introduces them. ``expected_id`` is only used
#: as the row identity when ``build()`` itself blows up before an instance (and its
#: real ``.source_id``) exists — the ordinary path always reports the adapter's own
#: ``source_id`` attribute, never this constant.
_PROBES: tuple[tuple[Callable[[RegionConfig], Any], str], ...] = (
    (_build_imd_bulletin, "imd_coastal_bulletin"),
    (_build_imd_geoserver, "imd_geoserver"),
    (_build_incois_wfs, "incois_wfs"),
    (_build_incois_osf, "incois_osf"),
    (_build_incois_argo, "incois_argo"),
    (_build_openmeteo_marine, "openmeteo_marine"),
    (_build_openmeteo_forecast, "openmeteo_forecast"),
    (_build_gdacs, "gdacs_tc"),
    (_build_marine_regions, "marine_regions_imbl"),
)


def _parse_iso(value: Any) -> datetime | None:
    """Best-effort ISO-8601 parse. ``None``/unparseable -> ``None``, never a guess."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _granule_age_seconds(health_info: dict[str, Any], now: datetime) -> float | None:
    """Age of the newest granule, from whichever timestamp ``health()`` actually gave
    us (``issued_at`` first, then ``time_coverage_end`` for the ERDDAP adapter).
    ``None`` when neither is present or parseable — never a fabricated age."""
    candidate = health_info.get("issued_at") or health_info.get("time_coverage_end")
    ts = _parse_iso(candidate)
    if ts is None:
        return None
    return max(0.0, (now - ts).total_seconds())


def _probe_source(
    build: Callable[[RegionConfig], Any], expected_id: str, region: RegionConfig, now: datetime
) -> dict[str, Any]:
    """Instantiate one adapter and call its own ``.health()``, isolated end to end.

    A source being unreachable, or the adapter's own ``.health()`` raising outright
    (rather than returning its usual ``ok=False`` row), is reported here as a normal
    row with ``ok=False`` — never allowed to propagate and take the other eight probes
    down with it. This is the layer of isolation above each adapter's own internal
    try/except (most already have one; this covers the case where they don't, or a
    test monkeypatches ``.health()`` itself to raise).
    """
    try:
        adapter = build(region)
    except Exception as exc:  # noqa: BLE001 — one adapter's import/init must not sink the rest
        log.warning("discovery: %s failed to construct: %s", expected_id, exc)
        return {
            "source_id": expected_id,
            "source_name": expected_id,
            "authority": "derived",
            "ok": False,
            "resolution_m": None,
            "cadence": _CADENCE.get(expected_id, "cadence not documented"),
            "granule_age_seconds": None,
            "count": 0,
            "latency_ms": None,
            "issued_at": None,
            "error": f"{type(exc).__name__}: {exc}",
            "url": _fallback_url(expected_id),
        }

    try:
        info = adapter.health()
    except Exception as exc:  # noqa: BLE001 — see docstring
        log.warning("discovery: %s.health() raised: %s", adapter.source_id, exc)
        info = {
            "ok": False,
            "count": 0,
            "latency_ms": None,
            "issued_at": None,
            "resolution_m": adapter.spatial_resolution_m,
            "error": f"{type(exc).__name__}: {exc}",
        }

    url = getattr(adapter, "base_url", "") or getattr(adapter, "url", "") or _fallback_url(
        adapter.source_id
    )
    resolution_m = info.get("resolution_m", adapter.spatial_resolution_m)
    return {
        "source_id": adapter.source_id,
        "source_name": adapter.source_name,
        "authority": adapter.authority,
        "ok": bool(info.get("ok")),
        "resolution_m": resolution_m,
        "cadence": _CADENCE.get(adapter.source_id, "cadence not documented"),
        "granule_age_seconds": _granule_age_seconds(info, now),
        "count": info.get("count") if isinstance(info.get("count"), (int, float)) else 0,
        "latency_ms": info.get("latency_ms"),
        "issued_at": info.get("issued_at"),
        "error": info.get("error"),
        "url": url,
    }


@registry.tool(
    name="list_available_data",
    number=16,
    description=(
        "What marine data actually exists for this region right now and how good it "
        "is — source, authority, spatial resolution, update cadence, and the age of "
        "the newest granule, per source adapter. This never fetches marine data "
        "itself; it is honest coverage reporting only. A source being unreachable or "
        "reporting a genuine gap (e.g. zero granules) is returned as a normal, named "
        "finding, never hidden or treated as a tool failure."
    ),
    schema={"properties": {}, "required": []},
    specialists=("MarineDataDiscovery",),
    reads_sources=(
        "imd_coastal_bulletin",
        "imd_geoserver",
        "incois_wfs",
        "incois_osf",
        "incois_argo",
        "openmeteo_marine",
        "openmeteo_forecast",
        "gdacs_tc",
        "marine_regions_imbl",
    ),
    cost="slow",
)
def list_available_data() -> ToolResult:
    """Probe every source adapter's own ``.health()`` and report coverage honestly.

    Never mutates ``FORESHORE_MODE`` — every adapter probed here respects whatever
    mode the process is already running under (fixture replay in tests/demo, live
    network otherwise), exactly as ``Source.get`` already enforces.
    """
    try:
        region = load_region()
    except Exception as exc:  # noqa: BLE001 — only a failure in the tool's own logic sinks it
        return ToolResult(
            tool="list_available_data",
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            summary="could not load region config; no source coverage could be probed",
        )

    now = utcnow()
    rows = [_probe_source(build, expected_id, region, now) for build, expected_id in _PROBES]

    lat0, lon0 = region.centre
    observations: list[Observation] = []
    for row in rows:
        prov = Provenance(
            source_id=row["source_id"],
            source_name=row["source_name"] or row["source_id"],
            authority=row["authority"] or "derived",
            url=row["url"],
            acquired_at=now,
            issued_at=_parse_iso(row["issued_at"]),
            spatial_resolution_m=row["resolution_m"],
            is_derived=False,
            notes=(
                f"coverage probe via {row['source_id']}.health()"
                + (f"; {row['error']}" if row["error"] else "; reachable")
            ),
        )
        observations.append(
            Observation(
                variable="data_coverage",
                value=row["count"],
                unit="granules",
                lat=lat0,
                lon=lon0,
                valid_time=prov.issued_at or now,
                provenance=prov,
                qualifiers={
                    "source_name": row["source_name"],
                    "authority": row["authority"],
                    "spatial_resolution_m": row["resolution_m"],
                    "update_cadence": row["cadence"],
                    "granule_age_seconds": row["granule_age_seconds"],
                    "ok": row["ok"],
                    "error": row["error"],
                    "latency_ms": row["latency_ms"],
                },
            )
        )

    payload_rows = [
        {
            "source_id": row["source_id"],
            "ok": row["ok"],
            "authority": row["authority"],
            "resolution_m": row["resolution_m"],
            "cadence": row["cadence"],
            "granule_age_seconds": row["granule_age_seconds"],
            "error": row["error"],
        }
        for row in rows
    ]

    ok_count = sum(1 for row in rows if row["ok"])
    total = len(rows)
    if ok_count == total:
        summary = f"{ok_count} of {total} data sources reachable."
    else:
        failing = ", ".join(row["source_id"] for row in rows if not row["ok"])
        summary = f"{ok_count} of {total} data sources reachable; unreachable/gap: {failing}."

    return ToolResult(
        tool="list_available_data",
        ok=True,
        observations=observations,
        payload={"sources": payload_rows},
        summary=summary,
    )


__all__ = ["list_available_data"]
