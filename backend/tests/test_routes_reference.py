"""Tests for ``foreshore.api.routes_reference`` — the "Reference and explainability"
section of ``docs/API.md``.

Deliberately does not import ``foreshore.api.main`` (its lifespan starts the push-loop
background thread on import, which a narrow router test does not want). Instead this
builds a throwaway ``FastAPI`` app with only ``routes_reference.router`` mounted, and
sets ``app.state.traces`` itself (mirroring what ``api/main.py``'s lifespan normally
does) for the two trace endpoints.

``FORESHORE_MODE=fixture`` is already forced for the whole session by
``tests/conftest.py``; nothing here touches the env var.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from foreshore.api.routes_reference import router
from foreshore.store.traces import TraceStore, new_step


def _build_app(tmp_path) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.traces = TraceStore(path=tmp_path / "traces.jsonl")
    return app


def _seed_trace(store: TraceStore) -> str:
    query_id = "test-query-1"
    root = new_step(
        query_id,
        agent="Planner",
        kind="plan",
        why="seed trace for routes_reference tests",
    )
    store.append(root)
    child = new_step(
        query_id,
        agent="OceanAnalytics",
        kind="tool_call",
        tool="get_sea_state",
        args={"lat": 9.28, "lon": 79.31},
        parent_id=root.step_id,
    )
    store.append(child)
    return query_id


# ------------------------------------------------------------------------------------
# GET /api/region
# ------------------------------------------------------------------------------------


def test_get_region_default(tmp_path):
    client = TestClient(_build_app(tmp_path))
    resp = client.get("/api/region")
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "region_id", "display_name_en", "display_name_local", "bbox", "anchor_ports",
        "primary_language", "fallback_language", "languages", "districts", "basemap",
        "vessel_classes",
    ):
        assert key in body
    assert body["region_id"] == "palk_bay_gom"
    assert isinstance(body["bbox"], list) and len(body["bbox"]) == 4
    assert len(body["vessel_classes"]) > 0
    vc = body["vessel_classes"][0]
    for key in (
        "class_id", "label_en", "label_local", "range_nm", "loa_m",
        "cruise_speed_kn", "max_speed_kn", "min_depth_m", "crew_typical",
    ):
        assert key in vc


def test_get_region_explicit_known_id(tmp_path):
    client = TestClient(_build_app(tmp_path))
    resp = client.get("/api/region", params={"region_id": "gujarat_sir_creek"})
    assert resp.status_code == 200
    assert resp.json()["region_id"] == "gujarat_sir_creek"


def test_get_region_unknown_id_is_404(tmp_path):
    client = TestClient(_build_app(tmp_path))
    resp = client.get("/api/region", params={"region_id": "nonexistent"})
    assert resp.status_code == 404
    assert "nonexistent" in resp.json()["detail"]


# ------------------------------------------------------------------------------------
# GET /api/architecture
# ------------------------------------------------------------------------------------


def test_get_architecture(tmp_path):
    client = TestClient(_build_app(tmp_path))
    resp = client.get("/api/architecture")
    assert resp.status_code == 200
    body = resp.json()
    assert "specialists" in body
    assert len(body["specialists"]) > 0
    row = body["specialists"][0]
    for key in ("name", "role", "ps_capability", "tools"):
        assert key in row


# ------------------------------------------------------------------------------------
# GET /api/catalogue
# ------------------------------------------------------------------------------------


def test_get_catalogue(tmp_path):
    client = TestClient(_build_app(tmp_path))
    resp = client.get("/api/catalogue")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("tool", "ok", "summary", "observations", "payload"):
        assert key in body
    assert "sources" in body["payload"]
    assert len(body["payload"]["sources"]) == 9


# ------------------------------------------------------------------------------------
# GET /api/traces, GET /api/trace/{query_id}
# ------------------------------------------------------------------------------------


def test_get_traces_and_trace_tree(tmp_path):
    app = _build_app(tmp_path)
    query_id = _seed_trace(app.state.traces)
    client = TestClient(app)

    resp = client.get("/api/traces", params={"limit": 20})
    assert resp.status_code == 200
    body = resp.json()
    assert "queries" in body
    assert any(q["query_id"] == query_id for q in body["queries"])

    resp2 = client.get(f"/api/trace/{query_id}")
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["query_id"] == query_id
    assert "steps" in body2
    assert len(body2["steps"]) >= 1


def test_get_trace_unknown_query_id_is_404(tmp_path):
    client = TestClient(_build_app(tmp_path))
    resp = client.get("/api/trace/no-such-query-id")
    assert resp.status_code == 404


# ------------------------------------------------------------------------------------
# GET /api/layers, GET /api/layers/{layer_id}
# ------------------------------------------------------------------------------------


def test_get_layers(tmp_path):
    client = TestClient(_build_app(tmp_path))
    resp = client.get("/api/layers")
    assert resp.status_code == 200
    body = resp.json()
    assert "layers" in body
    assert len(body["layers"]) > 0
    row = body["layers"][0]
    assert "layer_id" in row


def test_get_layer_known_id_returns_geojson(tmp_path):
    client = TestClient(_build_app(tmp_path))
    layers_resp = client.get("/api/layers")
    layer_id = layers_resp.json()["layers"][0]["layer_id"]

    resp = client.get(f"/api/layers/{layer_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert "features" in body
    if body["features"]:
        feat = body["features"][0]
        assert feat["type"] == "Feature"
        assert "geometry" in feat
        assert "properties" in feat


def test_get_layer_unknown_id_is_404(tmp_path):
    client = TestClient(_build_app(tmp_path))
    resp = client.get("/api/layers/no_such_layer")
    assert resp.status_code == 404


# ------------------------------------------------------------------------------------
# GET /api/geofences.geojson
# ------------------------------------------------------------------------------------


def test_get_geofences_geojson_no_filter(tmp_path):
    client = TestClient(_build_app(tmp_path))
    resp = client.get("/api/geofences.geojson")
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert "features" in body


def test_get_geofences_geojson_filtered_to_mpa_only(tmp_path):
    client = TestClient(_build_app(tmp_path))
    resp = client.get("/api/geofences.geojson", params={"classes": "MPA"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    for feat in body["features"]:
        assert feat["properties"]["geofence_class"] == "MPA"


def test_get_geofences_geojson_region_id_param(tmp_path):
    client = TestClient(_build_app(tmp_path))
    resp = client.get(
        "/api/geofences.geojson", params={"region_id": "palk_bay_gom", "classes": "MPA"}
    )
    assert resp.status_code == 200
    body = resp.json()
    for feat in body["features"]:
        assert feat["properties"]["geofence_class"] == "MPA"
