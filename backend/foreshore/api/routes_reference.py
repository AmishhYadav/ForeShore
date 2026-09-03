"""Reference and explainability REST surface — the last table in ``docs/API.md``'s
"Reference and explainability" section (``/health`` itself is wired directly in
``api/main.py``; every other row in that table lives here).

Pure wiring. Every endpoint below calls a helper that already exists elsewhere in the
codebase — ``config.load_region``/``load_vessels``, ``agents.specialists.architecture``,
``tools.discovery.list_available_data``, the shared ``TraceStore``/``VectorStore``, and
``geofence.engine.GeofenceEngine`` — and reshapes the result into the JSON shape
``docs/API.md`` promises. No computation happens in this module.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..config import load_region, load_vessels, set_active_region
from ..geofence.engine import GeofenceEngine
from ..models import GeofenceClass
from ..store.vectors import VectorStore
from ..tools.discovery import list_available_data
from ..agents.specialists import architecture as specialists_architecture

router = APIRouter(prefix="/api", tags=["reference"])


# ------------------------------------------------------------------------------------
# GET /api/region?region_id=
# ------------------------------------------------------------------------------------


def _region_dict(region) -> dict[str, Any]:
    vessels = load_vessels()
    return {
        "region_id": region.region_id,
        "display_name_en": region.display_name_en,
        "display_name_local": region.display_name_local,
        "bbox": list(region.bbox),
        "anchor_ports": [
            {"name": p.name, "lat": p.lat, "lon": p.lon, "district": p.district}
            for p in region.anchor_ports
        ],
        "primary_language": region.primary_language,
        "fallback_language": region.fallback_language,
        "languages": list(region.languages),
        "districts": list(region.districts),
        "basemap": region.basemap,
        "vessel_classes": [
            {
                "class_id": v.class_id,
                "label_en": v.label_en,
                "label_local": v.label_local,
                "range_nm": v.range_nm,
                "loa_m": v.loa_m,
                "cruise_speed_kn": v.cruise_speed_kn,
                "max_speed_kn": v.max_speed_kn,
                "min_depth_m": v.min_depth_m,
                "crew_typical": v.crew_typical,
            }
            for v in vessels.classes.values()
        ],
    }


@router.get("/region")
def get_region(region_id: str | None = None) -> dict[str, Any]:
    try:
        region = load_region(region_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _region_dict(region)


# ------------------------------------------------------------------------------------
# POST /api/region/active — live region swap (CLAUDE.md: "does this only work for
# Tamil Nadu?" answered by a live config file swap, not a redeploy)
#
# This mutates process-wide state (FORESHORE_REGION) via config.set_active_region,
# which is deliberately simple and matches this app's actual deployment shape: one
# backend process per demo, not a multi-tenant server serving several regions to
# different users at once. Every *query* can already be pointed at a specific region
# without any global mutation (POST /api/query's region_id field, unaffected by this
# endpoint) — this endpoint exists only so the map/fleet/geofence surfaces that don't
# take a region_id on every call can be told, once, which region they are now showing.
# ------------------------------------------------------------------------------------


class SetActiveRegionRequest(BaseModel):
    region_id: str


@router.post("/region/active")
def post_region_active(body: SetActiveRegionRequest) -> dict[str, Any]:
    try:
        region = set_active_region(body.region_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _region_dict(region)


# ------------------------------------------------------------------------------------
# GET /api/architecture
# ------------------------------------------------------------------------------------


@router.get("/architecture")
def get_architecture() -> dict[str, Any]:
    return {"specialists": specialists_architecture()}


# ------------------------------------------------------------------------------------
# GET /api/catalogue
# ------------------------------------------------------------------------------------


@router.get("/catalogue")
def get_catalogue() -> dict[str, Any]:
    result = list_available_data()
    return result.to_dict()


# ------------------------------------------------------------------------------------
# GET /api/traces?limit=20
# ------------------------------------------------------------------------------------


@router.get("/traces")
def get_traces(request: Request, limit: int = 20) -> dict[str, Any]:
    store = request.app.state.traces
    return {"queries": store.recent_queries(limit)}


# ------------------------------------------------------------------------------------
# GET /api/trace/{query_id}
# ------------------------------------------------------------------------------------


@router.get("/trace/{query_id}")
def get_trace(query_id: str, request: Request) -> dict[str, Any]:
    store = request.app.state.traces
    tree = store.tree(query_id)
    if not tree:
        raise HTTPException(status_code=404, detail=f"no trace for query_id {query_id!r}")
    return {"query_id": query_id, "steps": tree}


# ------------------------------------------------------------------------------------
# GET /api/layers
# ------------------------------------------------------------------------------------


@router.get("/layers")
def get_layers() -> dict[str, Any]:
    store = VectorStore()
    layers: list[dict[str, Any]] = []
    for layer_id in store.layers():
        try:
            meta = store.layer_meta(layer_id)
        except Exception:  # noqa: BLE001 — one corrupt sidecar must not 500 the listing
            meta = {}
        layers.append({"layer_id": layer_id, **meta})
    return {"layers": layers}


# ------------------------------------------------------------------------------------
# GET /api/layers/{layer_id}
# ------------------------------------------------------------------------------------


@router.get("/layers/{layer_id}")
def get_layer(layer_id: str) -> dict[str, Any]:
    store = VectorStore()
    if layer_id not in store.layers():
        raise HTTPException(status_code=404, detail=f"no such layer {layer_id!r}")
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": f.geometry, "properties": f.properties}
            for f in store.read_layer(layer_id)
        ],
    }


# ------------------------------------------------------------------------------------
# GET /api/geofences.geojson?classes=&region_id=
# ------------------------------------------------------------------------------------


@router.get("/geofences.geojson")
def get_geofences_geojson(
    classes: str | None = None, region_id: str | None = None
) -> dict[str, Any]:
    parsed: list[GeofenceClass] | None = None
    if classes:
        parsed = [c.strip() for c in classes.split(",") if c.strip()]  # type: ignore[misc]

    try:
        region = load_region(region_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    engine = GeofenceEngine(region=region)
    return engine.as_geojson(classes=parsed)


__all__ = ["router"]
