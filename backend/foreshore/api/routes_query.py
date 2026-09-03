"""The request path over HTTP.

``POST /api/query`` is the whole agent path (plan -> specialists -> verdict -> ceiling ->
synthesis), wired straight to :func:`foreshore.agents.orchestrator.answer`. The other
three endpoints here are thin single-tool passthroughs — ``docs/API.md`` calls
``/api/route`` "a thin passthrough to tool 11", and the same is true of
``/api/verdict`` (tool 15) and ``/api/geofence/check`` (tool 9): each calls the tool
function directly (the ``@registry.tool`` decorator returns the function itself, so
``plan_route(...)``, ``evaluate_verdict(...)`` and ``check_geofences(...)`` are callable
exactly like any other Python function) and returns its ``ToolResult`` shape unmodified,
so the boat UI can refresh a single card without paying for a full agent turn.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..agents.orchestrator import Query, answer as run_query
from ..tools.geofence_tools import check_geofences
from ..tools.hazards import get_hazard_alerts
from ..tools.pfz import find_nearest_pfz
from ..tools.pfz_derived import derive_pfz_zones
from ..tools.routing_tools import plan_route
from ..tools.verdict_tools import evaluate_verdict
from .serialize import tool_result_response


def _parse_bbox(bbox: str | None) -> list[float] | None:
    """``"78.0,8.0,80.6,10.9"`` -> ``[78.0, 8.0, 80.6, 10.9]``. Absent/unparsable both mean
    "use the active region's own bbox" — the tool layer already defaults that way."""
    if not bbox:
        return None
    try:
        parts = [float(p) for p in bbox.split(",")]
    except ValueError:
        return None
    return parts if len(parts) == 4 else None

router = APIRouter(prefix="/api", tags=["query"])


def _parse_iso(value: str | None) -> datetime | None:
    """Tolerant ISO-8601 parse. Unparsable/absent both fall back to ``None`` — the
    downstream module (planner, tool) decides what "no time given" means, never guessed
    here."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ------------------------------------------------------------------------------------
# POST /api/query
# ------------------------------------------------------------------------------------


class QueryRequest(BaseModel):
    text: str
    lat: float | None = None
    lon: float | None = None
    when: str | None = None
    vessel_class: str | None = None
    heading_deg: float | None = None
    speed_kn: float | None = None
    destination: tuple[float, float] | None = None
    #: ``None`` = auto-detect and mirror. Never a dropdown — PS bullet 2 is explicit.
    language: str | None = None
    region_id: str | None = None
    surface: str = "boat"
    use_model: bool = True


@router.post("/query")
def post_query(body: QueryRequest, request: Request) -> dict[str, Any]:
    query = Query(
        text=body.text,
        lat=body.lat,
        lon=body.lon,
        when=_parse_iso(body.when),
        vessel_class=body.vessel_class,
        heading_deg=body.heading_deg,
        speed_kn=body.speed_kn,
        destination=tuple(body.destination) if body.destination else None,
        language=body.language,
        region_id=body.region_id,
        surface="console" if body.surface == "console" else "boat",
        use_model=body.use_model,
    )
    outcome = run_query(query, traces=request.app.state.traces)
    # QueryOutcome.to_dict() is already fully JSON-safe (every nested object is its own
    # .to_dict()) — see models.AgentAnswer/Verdict/Observation/TraceStep.to_dict().
    return outcome.to_dict()


# ------------------------------------------------------------------------------------
# POST /api/route — thin passthrough to tool 11, plan_route
# ------------------------------------------------------------------------------------


class RouteRequest(BaseModel):
    origin_lat: float
    origin_lon: float
    dest_lat: float
    dest_lon: float
    departure: str | None = None
    vessel_class: str | None = None


@router.post("/route")
def post_route(body: RouteRequest) -> dict[str, Any]:
    result = plan_route(
        origin=[body.origin_lat, body.origin_lon],
        destination=[body.dest_lat, body.dest_lon],
        departure=body.departure,
        vessel_class=body.vessel_class,
    )
    return tool_result_response(result)


# ------------------------------------------------------------------------------------
# GET /api/verdict — thin passthrough to tool 15, evaluate_verdict
# ------------------------------------------------------------------------------------


@router.get("/verdict")
def get_verdict(
    lat: float,
    lon: float,
    vessel_class: str | None = None,
    when: str | None = None,
) -> dict[str, Any]:
    result = evaluate_verdict(lat=lat, lon=lon, vessel_class=vessel_class, when=when)
    return tool_result_response(result)


# ------------------------------------------------------------------------------------
# POST /api/geofence/check — thin passthrough to tool 9, check_geofences
# ------------------------------------------------------------------------------------


class GeofenceCheckRequest(BaseModel):
    lat: float
    lon: float
    heading_deg: float | None = None
    speed_kn: float | None = None
    classes: list[str] | None = None


@router.post("/geofence/check")
def post_geofence_check(body: GeofenceCheckRequest) -> dict[str, Any]:
    result = check_geofences(
        lat=body.lat,
        lon=body.lon,
        heading_deg=body.heading_deg,
        speed_kn=body.speed_kn,
        classes=body.classes,
    )
    return tool_result_response(result)



# ------------------------------------------------------------------------------------
# GET /api/pfz/official?lat&lon — thin passthrough to tool 7, find_nearest_pfz
#
# Map-layer passthroughs (this + the two below): the boat/console maps need PFZ lines,
# derived zones and hazard geometry to draw whether or not the user has asked a
# question, so — like /api/route, /api/verdict and /api/geofence/check above — these
# call the tool directly rather than paying for a full agent turn.
# ------------------------------------------------------------------------------------


@router.get("/pfz/official")
def get_pfz_official(lat: float, lon: float) -> dict[str, Any]:
    return tool_result_response(find_nearest_pfz(lat=lat, lon=lon))


# ------------------------------------------------------------------------------------
# GET /api/pfz/derived?bbox&when — thin passthrough to tool 8, derive_pfz_zones
# ------------------------------------------------------------------------------------


@router.get("/pfz/derived")
def get_pfz_derived(bbox: str | None = None, when: str | None = None) -> dict[str, Any]:
    return tool_result_response(derive_pfz_zones(bbox=_parse_bbox(bbox), when=when))


# ------------------------------------------------------------------------------------
# GET /api/hazards?bbox&when — thin passthrough to tool 12, get_hazard_alerts
#
# payload carries `polygons` (cone/wind-radii exclusion areas) and `cyclone_track`
# (the storm's own observed+forecast LineString) as two distinct GeoJSON collections —
# see tools/hazards.py's module docstring. Zero active hazards is `ok=True` with
# `payload["no_active_hazard"] = True`, never an error.
# ------------------------------------------------------------------------------------


@router.get("/hazards")
def get_hazards(bbox: str | None = None, when: str | None = None) -> dict[str, Any]:
    return tool_result_response(get_hazard_alerts(bbox=_parse_bbox(bbox), when=when))


__all__ = ["router"]
