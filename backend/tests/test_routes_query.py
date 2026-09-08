"""Tests for ``GET /api/productive-waters`` — the thin passthrough to tool 18,
``find_productive_waters`` (``backend/foreshore/api/routes_query.py``).

Mirrors ``test_routes_reference.py``'s convention: a throwaway ``FastAPI`` app with only
the router mounted (``routes_query.router`` here), rather than importing
``foreshore.api.main`` (whose lifespan starts the push-loop background thread on import —
not wanted for a narrow route test).

Both adapters ``find_productive_waters`` reads are monkeypatched at the class boundary —
the same approach ``test_productive_waters.py`` uses for the tool itself — so these tests
exercise the *real* route handler and the *real* tool, never a stubbed-out function, while
staying deterministic and socket-free (``FORESHORE_MODE=fixture`` is already enforced
session-wide by ``conftest.py``, but this coast has no live NOAA CoastWatch fixture on
disk, so leaving the adapters unpatched would fall through to ``_missing_result`` for a
reason unrelated to what this file is testing).
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from foreshore.api.routes_query import router
from foreshore.models import Provenance, utcnow
from foreshore.sources.incois_thredds import GridSlice, IncoisThredds
from foreshore.sources.oceancolour import OceanColour


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


# --------------------------------------------------------------------------------------
# A tiny synthetic grid, same shape of fake ``test_productive_waters.py`` installs on the
# two adapter classes — small enough that one favourable 2x2 SST block plus uniform
# chlorophyll everywhere reliably produces exactly one ranked zone.
# --------------------------------------------------------------------------------------

_LATS = np.round(np.linspace(8.0, 9.8, 10), 4)
_LONS = np.round(np.linspace(78.0, 79.8, 10), 4)


def _prov(source_id: str, source_name: str, authority: str, url: str, resolution_m: float) -> Provenance:
    now = utcnow()
    return Provenance(
        source_id=source_id, source_name=source_name, authority=authority, url=url,
        acquired_at=now, issued_at=now, valid_from=now - timedelta(hours=1),
        valid_to=now + timedelta(hours=23), spatial_resolution_m=resolution_m,
    )


def _install_working_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    chl_arr = np.full((10, 10), 1.2)
    sst_arr = np.full((10, 10), 33.0)  # outside the favourable band everywhere...
    sst_arr[1:3, 1:3] = 27.0  # ...except one 2x2 block inside it.

    now = utcnow()
    today = now.date()
    chl_gs = GridSlice(
        product="chl", variables={"chlorophyll_a": chl_arr}, lats=_LATS, lons=_LONS,
        valid_time=now, file_date=today, local_path=None, history="fake gap-filled chlorophyll composite",
        provenance=_prov("noaa_coastwatch_fake", "fake NOAA CoastWatch chlorophyll", "NOAA",
                          "https://example.test/chl", 9_277.0),
    )
    sst_gs = GridSlice(
        product="sst", variables={"sea_surface_temperature": sst_arr}, lats=_LATS, lons=_LONS,
        valid_time=now, file_date=today, local_path=None, history="fake OSF SST model",
        provenance=_prov("incois_osf_sst_fake", "fake INCOIS OSF SST", "INCOIS",
                          "https://example.test/sst", 11_000.0),
    )

    monkeypatch.setattr(
        OceanColour, "chlorophyll_slice", lambda self, bbox, *, at=None, product="gapfilled": chl_gs
    )
    monkeypatch.setattr(
        IncoisThredds, "slice", lambda self, product, *, variables=None, at=None, bbox=None: sst_gs
    )


# ------------------------------------------------------------------------------------
# GET /api/productive-waters — success shape
# ------------------------------------------------------------------------------------


def test_get_productive_waters_returns_payload_shape(monkeypatch):
    _install_working_fakes(monkeypatch)
    client = TestClient(_build_app())

    resp = client.get(
        "/api/productive-waters",
        params={"bbox": "78.0,8.0,79.8,9.8", "lat": 8.0, "lon": 78.0, "limit": 5},
    )
    assert resp.status_code == 200
    body = resp.json()

    for key in ("tool", "ok", "summary", "observations", "payload", "error", "partial", "missing"):
        assert key in body
    assert body["tool"] == "find_productive_waters"
    assert body["ok"] is True

    payload = body["payload"]
    for key in (
        "zones", "reference_point", "method", "chlorophyll_source", "sst_source",
        "sst_band_degc", "chlorophyll_percentile",
    ):
        assert key in payload

    assert payload["zones"]["type"] == "FeatureCollection"
    assert len(payload["zones"]["features"]) == 1
    feature = payload["zones"]["features"][0]
    props = feature["properties"]
    assert props["is_derived"] is True  # never presented as the official INCOIS advisory
    for key in (
        "zone_id", "zone_rank", "mean_chlorophyll_mg_m3", "mean_sst_degc",
        "area_nm2", "distance_nm", "bearing_deg", "centroid_lat", "centroid_lon",
    ):
        assert key in props

    assert payload["chlorophyll_source"] == "fake NOAA CoastWatch chlorophyll"
    assert payload["sst_source"] == "fake INCOIS OSF SST"
    assert "INDICATIVE" in body["summary"]
    assert "INCOIS" in body["summary"]  # names the official product it is not


def test_get_productive_waters_defaults_bbox_when_omitted(monkeypatch):
    """No ``bbox`` query param -> the tool's own default (the active region's bbox), same
    contract as ``GET /api/pfz/derived`` with no ``bbox``."""
    _install_working_fakes(monkeypatch)
    client = TestClient(_build_app())

    resp = client.get("/api/productive-waters")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ------------------------------------------------------------------------------------
# GET /api/productive-waters — a source failure is a stated outcome, never a 500
# ------------------------------------------------------------------------------------


def test_get_productive_waters_chlorophyll_failure_is_stated_outcome_not_500(monkeypatch):
    def _boom(self, bbox, *, at=None, product="gapfilled"):
        raise RuntimeError("simulated NOAA CoastWatch outage")

    monkeypatch.setattr(OceanColour, "chlorophyll_slice", _boom)
    client = TestClient(_build_app())

    resp = client.get("/api/productive-waters", params={"bbox": "78.0,8.0,79.8,9.8"})
    assert resp.status_code == 200  # never a 500 — the tool abstains, it does not raise
    body = resp.json()
    assert body["ok"] is True
    assert body["partial"] is True
    assert body["missing"] == ["chlorophyll"]
    assert body["payload"]["zones"] == {"type": "FeatureCollection", "features": []}
    assert body["payload"]["chlorophyll_source"] is None


def test_get_productive_waters_sst_failure_is_stated_outcome_not_500(monkeypatch):
    chl_arr = np.full((10, 10), 1.2)
    now = utcnow()
    chl_gs = GridSlice(
        product="chl", variables={"chlorophyll_a": chl_arr}, lats=_LATS, lons=_LONS,
        valid_time=now, file_date=now.date(), local_path=None, history="fake chlorophyll composite",
        provenance=_prov("noaa_coastwatch_fake", "fake NOAA CoastWatch chlorophyll", "NOAA",
                          "https://example.test/chl", 9_277.0),
    )
    monkeypatch.setattr(
        OceanColour, "chlorophyll_slice", lambda self, bbox, *, at=None, product="gapfilled": chl_gs
    )

    def _boom(self, product, *, variables=None, at=None, bbox=None):
        raise RuntimeError("simulated INCOIS OSF outage")

    monkeypatch.setattr(IncoisThredds, "slice", _boom)
    client = TestClient(_build_app())

    resp = client.get("/api/productive-waters", params={"bbox": "78.0,8.0,79.8,9.8"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["partial"] is True
    assert body["missing"] == ["sea_surface_temperature"]
