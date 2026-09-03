"""Hazard alert tool — union of GDACS tropical-cyclone hazards and the IMD cyclone track.

Tool 12 (``get_hazard_alerts``) is deliberately framed so that "no active hazard" is a
first-class, positive answer rather than something that merely falls out of an empty
list: both upstream sources report a healthy, reachable "nothing to see" far more often
than they report an active storm (GDACS's own health check calls this
``no_active_cyclone``; IMD's ``Cyclone_Track_V`` layer returns 0 features whenever no
cyclone is active, which its own adapter module documents as "valid, not an error").

The two sources are read independently and degrade independently: if one is down but
the other answers, this tool still returns what it has, marked ``partial=True`` with the
failed source named in ``missing`` -- a missing input never becomes a bare failure when
half the answer survived.

``payload["polygons"]`` (cone + red/orange wind-radii, from GDACS) and
``payload["cyclone_track"]`` (a GeoJSON ``FeatureCollection`` of the storm's observed +
forecast ``LineString`` track, also from GDACS) are kept distinct on purpose: the console
map draws them as different things -- an avoidance area versus a path -- per PLAN.md
Phase 6's "cyclone track and cone overlaid". Both degrade to an empty-but-valid
collection when there is no active cyclone near the region, never an error.

Adapters (:class:`foreshore.sources.gdacs.GDACSCyclones`,
:class:`foreshore.sources.imd_geoserver.IMDGeoServer`) are imported lazily inside the
tool function so a half-written or temporarily failing adapter module cannot prevent
this module from registering its tool with the process-wide registry.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import Observation, ToolResult, utcnow
from .registry import registry

_GDACS_SOURCE_ID = "gdacs_tc"
_IMD_SOURCE_ID = "imd_geoserver"


def _parse_when(when: str | None) -> tuple[datetime | None, str | None]:
    """Tolerant ISO-8601 parse. Both upstream sources report *current* state only, so
    ``when`` is used purely to label the reference time in the summary -- an
    unparseable value degrades to "now" rather than failing the tool."""
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


def _in_bbox(lat: float | None, lon: float | None, bbox: tuple[float, float, float, float]) -> bool:
    if lat is None or lon is None:
        return False
    minlon, minlat, maxlon, maxlat = bbox
    return minlat <= lat <= maxlat and minlon <= lon <= maxlon


def _feature_overlaps_bbox(feat: dict[str, Any], bbox: tuple[float, float, float, float]) -> bool:
    """Cheap overlap test on a GeoJSON feature's own ``bbox`` property (GDACS geometry
    features carry one). Conservative: a feature with no usable bbox is kept rather
    than silently dropped."""
    fb = feat.get("bbox")
    if not (isinstance(fb, (list, tuple)) and len(fb) == 4):
        return True
    fminlon, fminlat, fmaxlon, fmaxlat = fb
    minlon, minlat, maxlon, maxlat = bbox
    return fmaxlon >= minlon and fminlon <= maxlon and fmaxlat >= minlat and fminlat <= maxlat


def _event_label(event: dict[str, Any]) -> str:
    name = event.get("name") or event.get("event_name")
    if name:
        return str(name)
    if "cyclone_id" in event:
        return f"IMD cyclone track {event.get('cyclone_id')}"
    return "unnamed hazard"


@registry.tool(
    name="get_hazard_alerts",
    number=12,
    description=(
        "Active tropical-cyclone hazard check: union of GDACS tropical-cyclone events "
        "near the region (with cone/wind-radii exclusion polygons in payload['polygons'] "
        "and the storm's own observed+forecast track as a GeoJSON LineString "
        "FeatureCollection in payload['cyclone_track']) and the IMD cyclone track. "
        "Zero active hazards is a valid, positive result, not an error -- use this to "
        "confirm there is no cyclone threat as much as to retrieve one that exists."
    ),
    schema={
        "type": "object",
        "properties": {
            "bbox": {
                "type": ["array", "null"],
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
                "description": (
                    "Optional [minlon, minlat, maxlon, maxlat] EPSG:4326 override. "
                    "Defaults to the active region's bbox. Note both upstream sources "
                    "scope their own primary query to the active region already; a "
                    "narrower bbox here only re-filters what they returned."
                ),
            },
            "when": {
                "type": ["string", "null"],
                "description": (
                    "Optional ISO-8601 reference time, for labelling only -- both "
                    "sources report current state. Omit or null for now."
                ),
            },
        },
        "required": [],
    },
    specialists=("WeatherIntelligence", "ReportingAgent"),
    reads_sources=(_GDACS_SOURCE_ID, _IMD_SOURCE_ID),
    cost="fast",
)
def get_hazard_alerts(
    bbox: list[float] | tuple[float, ...] | None = None, when: str | None = None
) -> ToolResult:
    """Union of GDACS tropical-cyclone hazards and the IMD cyclone track for ``bbox``
    (default: the active region's bbox).

    Never raises. Each source fails independently: if one is unreachable but the other
    answers, this returns what survived with ``partial=True`` and the failed source
    named in ``missing``. Only if BOTH fail does this return ``ok=False``. Zero hazards
    from both is reported as ``ok=True`` with ``payload["no_active_hazard"] = True`` --
    a designed, positive outcome.
    """
    from ..config import load_region

    region = load_region()
    active_bbox = tuple(float(v) for v in bbox) if bbox else region.bbox
    custom_bbox = bool(bbox)

    when_dt, parse_note = _parse_when(when)
    when_dt = when_dt or utcnow()

    observations: list[Observation] = []
    polygons: list[dict[str, Any]] = []
    cyclone_track_features: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    missing: list[str] = []
    errors: list[str] = []

    # -- GDACS tropical-cyclone events + hazard geometry --------------------------
    gdacs_ok = False
    try:
        from ..sources.gdacs import GDACSCyclones

        gdacs = GDACSCyclones(region=region)
        near_events = gdacs.events_near_region()
        polys, gdacs_obs = gdacs.exclusion_polygons()
        track_feats, track_obs = gdacs.track_lines()
        if custom_bbox:
            near_events = [e for e in near_events if _in_bbox(e.lat, e.lon, active_bbox)]
            polys = [p for p in polys if _feature_overlaps_bbox(p, active_bbox)]
            track_feats = [t for t in track_feats if _feature_overlaps_bbox(t, active_bbox)]
        polygons.extend(polys)
        cyclone_track_features.extend(track_feats)
        observations.extend(gdacs_obs)
        observations.extend(track_obs)
        events.extend(e.to_dict() for e in near_events)
        gdacs_ok = True
    except Exception as exc:  # noqa: BLE001 - one source failing must not sink the tool
        missing.append(_GDACS_SOURCE_ID)
        errors.append(f"GDACS ({_GDACS_SOURCE_ID}): {type(exc).__name__}: {exc}")

    # -- IMD cyclone track ----------------------------------------------------------
    imd_ok = False
    try:
        from ..sources.imd_geoserver import IMDGeoServer

        imd = IMDGeoServer(region=region)
        imd_obs = imd.parse_cyclone()
        if custom_bbox:
            imd_obs = [o for o in imd_obs if _in_bbox(o.lat, o.lon, active_bbox)]
        observations.extend(imd_obs)
        imd_ok = True
        if imd_obs:
            by_id: dict[Any, list[Observation]] = {}
            for o in imd_obs:
                by_id.setdefault(o.qualifiers.get("cyclone_id"), []).append(o)
            for cid, obs_list in by_id.items():
                sample = obs_list[0]
                events.append({
                    "source": _IMD_SOURCE_ID,
                    "cyclone_id": cid,
                    "cyclone_type": sample.qualifiers.get("cyclone_type"),
                    "track_points": len(obs_list),
                })
    except Exception as exc:  # noqa: BLE001
        missing.append(_IMD_SOURCE_ID)
        errors.append(f"IMD ({_IMD_SOURCE_ID}): {type(exc).__name__}: {exc}")

    # -- both sources unreachable: unknown, not confirmed clear ---------------------
    if not gdacs_ok and not imd_ok:
        return ToolResult(
            tool="get_hazard_alerts",
            ok=False,
            error="; ".join(errors),
            summary=(
                "Could not reach either hazard source (GDACS tropical-cyclone events or "
                "the IMD cyclone track); hazard status is unknown, not confirmed clear."
            ),
            missing=missing,
            payload={
                "events": [],
                "polygons": [],
                "cyclone_track": {"type": "FeatureCollection", "features": []},
                "no_active_hazard": False,
            },
        )

    partial = bool(missing)
    no_active = len(events) == 0

    if no_active:
        checked = []
        if gdacs_ok:
            checked.append("GDACS tropical-cyclone event list")
        if imd_ok:
            checked.append("IMD cyclone track (imd:Cyclone_Track_V)")
        summary = (
            f"No active tropical cyclone affecting {region.display_name_en} as of "
            f"{when_dt.isoformat()}. Checked: {' and '.join(checked)}."
        )
        if partial:
            summary += f" One source could not be checked ({'; '.join(errors)})."
        payload: dict[str, Any] = {
            "events": [],
            "polygons": [],
            "cyclone_track": {"type": "FeatureCollection", "features": []},
            "no_active_hazard": True,
        }
    else:
        labels = sorted({_event_label(e) for e in events})
        summary = (
            f"{len(events)} tropical-cyclone hazard record(s) near "
            f"{region.display_name_en}: {', '.join(labels)}."
        )
        if partial:
            summary += f" One source failed to respond ({'; '.join(errors)}); showing the other."
        payload = {
            "events": events,
            "polygons": polygons,
            "cyclone_track": {"type": "FeatureCollection", "features": cyclone_track_features},
            "no_active_hazard": False,
        }

    if parse_note:
        summary += f" ({parse_note})"

    return ToolResult(
        tool="get_hazard_alerts",
        ok=True,
        observations=observations,
        payload=payload,
        summary=summary,
        partial=partial,
        missing=missing,
    )


__all__ = ["get_hazard_alerts"]
