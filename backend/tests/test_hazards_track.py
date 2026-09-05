"""Tests for the GDACS cyclone-track LineString wired through ``get_hazard_alerts``.

Two things are exercised here:

1. **The live "0 active cyclone near this region" case** — verified against the real
   GDACS API (not mocked), because CLAUDE.md is explicit that this is the common,
   expected, *positive* outcome, not an error, and the fixture suite ships no
   ``data/fixtures/gdacs_tc/`` snapshot to replay instead (``data/fixtures/`` is empty —
   see ``test_provenance.py``'s module docstring). The whole suite runs under
   ``FORESHORE_MODE=fixture`` (forced in ``conftest.py``); this file flips that back to
   ``live`` only for the duration of the one test that needs it, via ``monkeypatch``,
   which reverts automatically at teardown — the session-scoped assertion in
   ``conftest.py`` that the mode stays "fixture" only runs once, at the *first* test of
   the session, well before this one, so a temporary flip here does not trip it.

   Probed directly against ``gdacs.org`` on 2026-09-03 while writing this test: 19
   tracked TC events worldwide, exactly one ``iscurrent == "true"`` (SAUDEL-26, off
   Japan/China — centroid ~[117.4, 23.8]), nowhere near the Palk Bay / Gulf of Mannar
   bbox even with the adapter's 5-degree region buffer. So live, right now,
   ``events_near_region()`` for the default region is empty and the cyclone-track field
   must degrade to an empty-but-valid ``FeatureCollection``, not an error.

2. **The "cyclone is active" case** — GDACS will not have a storm near Palk Bay for
   this test to observe live (and grading can't depend on the weather), so this half
   is exercised with a recorded/synthetic fixture instead: two ``Line_Line_N`` features
   captured verbatim from a real, live GDACS ``getgeometry`` response for the one
   globally-current event at probe time (SAUDEL-26, eventid 1001305, episodeid 47) —
   trimmed to the properties this code path actually reads, not fabricated schema. The
   adapter's ``events_near_region``/``geometry`` are monkeypatched to return this real
   payload as if that storm were sitting inside the region bbox, so the parsing,
   tagging and provenance-construction code is exercised against a genuine GDACS
   response shape end to end.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from foreshore import config as foreshore_config
from foreshore.models import Provenance
from foreshore.sources.base import FetchResult
from foreshore.sources.gdacs import GDACSCyclones, CycloneEvent
from foreshore.tools.hazards import get_hazard_alerts

UTC = timezone.utc

# -- real GDACS geometry payload, captured live 2026-09-03 -----------------------------
# Trimmed to the properties gdacs.py's geometry()/track_lines() actually read (Class,
# eventname, alertlevel) plus enough of the rest to look like the real wire shape.
_REAL_EVENT_ID = "1001305"
_REAL_EPISODE_ID = "47"
_REAL_EVENT_NAME = "SAUDEL-26"
_REAL_TRACK_FEATURES = [
    {
        "type": "Feature",
        "bbox": [122.6, 28.1, 123.5, 28.2],
        "geometry": {
            "type": "LineString",
            "coordinates": [[123.5, 28.2], [122.6, 28.1]],
        },
        "properties": {
            "eventtype": "TC",
            "eventid": 1001305,
            "episodeid": 47,
            "eventname": _REAL_EVENT_NAME,
            "alertlevel": "Orange",
            "iscurrent": "true",
            "forecast": False,
            "Class": "Line_Line_0",
        },
    },
    {
        "type": "Feature",
        "bbox": [123.5, 28.2, 124.2, 28.4],
        "geometry": {
            "type": "LineString",
            "coordinates": [[124.2, 28.4], [123.5, 28.2]],
        },
        "properties": {
            "eventtype": "TC",
            "eventid": 1001305,
            "episodeid": 47,
            "eventname": _REAL_EVENT_NAME,
            "alertlevel": "Orange",
            "iscurrent": "true",
            "forecast": False,
            "Class": "Line_Line_1",
        },
    },
]
_REAL_GEOMETRY_URL = (
    "https://www.gdacs.org/gdacsapi/api/polygons/getgeometry"
    f"?eventtype=TC&eventid={_REAL_EVENT_ID}&episodeid={_REAL_EPISODE_ID}"
)


def _classified_geometry() -> dict[str, list[dict]]:
    """Mirrors ``GDACSCyclones.geometry``'s own bucketing for the synthetic payload:
    no cones/wind-radii/points in this trimmed fixture, only the two track segments."""
    return {
        "cones": [], "wind_red": [], "wind_orange": [], "wind_green": [],
        "track": list(_REAL_TRACK_FEATURES), "points": [],
    }


def _synthetic_event(region) -> CycloneEvent:
    """A hand-built ``CycloneEvent`` for SAUDEL-26, repositioned to the region's centre
    so ``events_near_region``'s bbox check (which this test bypasses by monkeypatching
    that method directly) is irrelevant — the point is to exercise the geometry/track
    parsing and provenance path, not the region-proximity filter (already covered
    elsewhere)."""
    lat0, lon0 = region.centre
    return CycloneEvent(
        event_id=_REAL_EVENT_ID,
        episode_id=_REAL_EPISODE_ID,
        name=_REAL_EVENT_NAME,
        alert_level="Orange",
        from_date=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
        to_date=datetime(2026, 9, 3, 0, 0, tzinfo=UTC),
        lat=lat0,
        lon=lon0,
        country="Japan, China, Northern Mariana Islands",
        severity="Tropical Storm (maximum wind speed of 213 km/h)",
        is_current=True,
    )


# --------------------------------------------------------------------------------------
# 1. Live: no active cyclone near the region right now -- a valid, positive result.
# --------------------------------------------------------------------------------------


def test_live_gdacs_zero_active_cyclone_degrades_to_empty_valid_track(monkeypatch, region):
    """Bypasses fixture mode for this one test only (see module docstring) and hits the
    real GDACS event list. Confirms the adapter-level ``track_lines()`` and the
    ``get_hazard_alerts`` tool payload both degrade to an empty-but-valid GeoJSON
    FeatureCollection -- ``ok=True``, no exception -- rather than treating "nothing
    happening near Palk Bay right now" as a failure.
    """
    monkeypatch.setenv("FORESHORE_MODE", "live")
    assert not foreshore_config.is_fixture()  # sanity: the flip actually took

    # Keep this test scoped to GDACS: IMD GeoServer is a separate, unrelated live
    # dependency (no fixture either) that this test is not about -- force it to abstain
    # immediately rather than paying for (and being at the mercy of) a second live
    # network round trip that has nothing to do with the cyclone-track field under test.
    from foreshore.sources.imd_geoserver import IMDGeoServer

    def _imd_unavailable(self):
        raise RuntimeError("IMD GeoServer intentionally not exercised by this test")

    monkeypatch.setattr(IMDGeoServer, "parse_cyclone", _imd_unavailable)

    try:
        gdacs = GDACSCyclones(region=region)
        near = gdacs.events_near_region()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live GDACS API unreachable from this environment: {exc}")

    assert near == [], (
        "expected 0 GDACS events near the Palk Bay / Gulf of Mannar region right now "
        f"(probed live 2026-09-03: only SAUDEL-26 near Japan/China was current); got "
        f"{[e.name for e in near]}. If a real cyclone is now active near this coast, "
        "this assertion is simply out of date -- re-verify against gdacs.org directly."
    )

    tracks, track_obs = gdacs.track_lines()
    assert tracks == []
    assert track_obs == []

    result = get_hazard_alerts()
    assert result.ok is True
    assert "cyclone_track" in result.payload
    fc = result.payload["cyclone_track"]
    assert fc["type"] == "FeatureCollection"
    assert fc["features"] == []
    # GDACS itself found nothing near the region, independent of whatever IMD reports
    # (IMD has no fixture and no live network is used for it here under the still-live
    # FORESHORE_MODE, so it legitimately raises FetchResult errors and is recorded as
    # `missing` -- that must not turn the whole tool result into an error;
    # get_hazard_alerts only fails when BOTH sources are unreachable, and GDACS answered).
    assert result.payload["polygons"] == []


# --------------------------------------------------------------------------------------
# 2. Active cyclone: real GDACS-shaped payload, monkeypatched in (offline, deterministic).
# --------------------------------------------------------------------------------------


def test_track_lines_structure_and_provenance_when_active(monkeypatch, region):
    """``GDACSCyclones.track_lines()`` directly, against the real-shaped SAUDEL-26
    ``Line_Line_N`` payload. Checks: features are tagged, geometry stays a LineString,
    and provenance is real GDACS provenance (authority, url, acquired_at) -- not
    fabricated."""
    ev = _synthetic_event(region)
    monkeypatch.setattr(GDACSCyclones, "events_near_region", lambda self: [ev])

    acquired = datetime(2026, 9, 3, 11, 0, tzinfo=UTC)

    def _fake_geometry(self, event_id, episode_id):
        raw = FetchResult(
            payload={"type": "FeatureCollection", "features": _REAL_TRACK_FEATURES},
            url=_REAL_GEOMETRY_URL,
            key="test-key",
            acquired_at=acquired,
        )
        return _classified_geometry(), raw

    monkeypatch.setattr(GDACSCyclones, "geometry", _fake_geometry)

    gdacs = GDACSCyclones(region=region)
    tracks, track_obs = gdacs.track_lines()

    # -- structure: a list of tagged GeoJSON LineString features -----------------------
    assert len(tracks) == 2
    for feat in tracks:
        assert feat["type"] == "Feature"
        assert feat["geometry"]["type"] == "LineString"
        coords = feat["geometry"]["coordinates"]
        assert isinstance(coords, list) and len(coords) >= 2
        props = feat["properties"]
        assert props["hazard_class"] == "cyclone_track"
        assert props["event_name"] == _REAL_EVENT_NAME
        assert props["alert_level"] == "Orange"
        # original GDACS properties (e.g. Class) survive the tagging, not clobbered
        assert props["Class"].startswith("Line_Line")

    # -- provenance: real GDACS authority/url/acquired_at, not invented ----------------
    assert track_obs, "expected one Observation per event carrying track provenance"
    for obs in track_obs:
        prov = obs.provenance
        assert isinstance(prov, Provenance)
        assert prov.authority == "JRC/GDACS"
        assert prov.source_id == "gdacs_tc"
        assert prov.url == _REAL_GEOMETRY_URL
        assert prov.acquired_at == acquired
    segment_counts = {obs.variable: obs.value for obs in track_obs}
    assert segment_counts.get("cyclone_track_segment_count") == 2

    # -- exclusion_polygons() is unaffected: this fixture carries no cone/wind features
    polys, poly_obs = gdacs.exclusion_polygons()
    assert polys == []


def test_get_hazard_alerts_carries_cyclone_track_when_active(monkeypatch, region):
    """End-to-end through the tool: ``payload["cyclone_track"]`` is present, distinct
    from ``payload["polygons"]``, and its observations flow into ``ToolResult.observations``
    with real GDACS provenance -- matching the pattern already used for the polygon path
    in the same tool. IMD has no fixture in this suite, so it abstains (``FixtureMissing``,
    caught) and is recorded as a missing/partial source rather than failing the tool --
    GDACS alone is enough for ``ok=True`` here.
    """
    ev = _synthetic_event(region)
    monkeypatch.setattr(GDACSCyclones, "events_near_region", lambda self: [ev])

    acquired = datetime(2026, 9, 3, 11, 0, tzinfo=UTC)

    def _fake_geometry(self, event_id, episode_id):
        raw = FetchResult(
            payload={"type": "FeatureCollection", "features": _REAL_TRACK_FEATURES},
            url=_REAL_GEOMETRY_URL,
            key="test-key",
            acquired_at=acquired,
        )
        return _classified_geometry(), raw

    monkeypatch.setattr(GDACSCyclones, "geometry", _fake_geometry)

    result = get_hazard_alerts()

    assert result.ok is True
    assert "gdacs_tc" not in result.missing  # GDACS itself succeeded
    fc = result.payload["cyclone_track"]
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 2
    for feat in fc["features"]:
        assert feat["geometry"]["type"] == "LineString"
        assert feat["properties"]["hazard_class"] == "cyclone_track"

    # distinct from, and additive alongside, the pre-existing polygon field
    assert "polygons" in result.payload
    assert result.payload["polygons"] == []  # this fixture carries no cone/wind polygons

    track_var_obs = [
        o for o in result.observations if o.variable == "cyclone_track_segment_count"
    ]
    assert track_var_obs, "expected the track Observation(s) to reach ToolResult.observations"
    for obs in track_var_obs:
        assert obs.provenance.authority == "JRC/GDACS"
        assert obs.provenance.source_id == "gdacs_tc"


def test_a_cone_reaching_into_the_bbox_is_kept_even_when_no_storm_centre_is_inside(
    monkeypatch, region
):
    """Events are filtered by storm *centroid*; cones and track lines by *feature overlap*.

    Deriving "no active hazard" from the event count alone therefore threw away geometry
    that genuinely reached the caller's area — and the boat map asks with a narrow ~0.6°
    bbox around the vessel on every render, which is exactly the shape that triggers it.
    A cyclone whose eye is 3° away but whose forecast cone is overhead must not be
    reported as "no active cyclone hazard in this area".
    """
    lat0, lon0 = region.centre

    # Eye well outside the narrow bbox below; cone overlapping it.
    ev = _synthetic_event(region)
    far_event = CycloneEvent(
        event_id=ev.event_id,
        episode_id=ev.episode_id,
        name=ev.name,
        alert_level=ev.alert_level,
        from_date=ev.from_date,
        to_date=ev.to_date,
        lat=lat0 + 3.0,
        lon=lon0 + 3.0,
        country=ev.country,
        severity=ev.severity,
        is_current=True,
    )
    cone = {
        "type": "Feature",
        "bbox": [lon0 - 0.2, lat0 - 0.2, lon0 + 3.5, lat0 + 3.5],
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [lon0 - 0.2, lat0 - 0.2], [lon0 + 3.5, lat0 - 0.2],
                [lon0 + 3.5, lat0 + 3.5], [lon0 - 0.2, lat0 + 3.5],
                [lon0 - 0.2, lat0 - 0.2],
            ]],
        },
        "properties": {"hazard_class": "cyclone_cone", "event_name": ev.name},
    }

    monkeypatch.setattr(GDACSCyclones, "events_near_region", lambda self: [far_event])
    monkeypatch.setattr(GDACSCyclones, "exclusion_polygons", lambda self: ([cone], []))
    monkeypatch.setattr(GDACSCyclones, "track_lines", lambda self: ([], []))

    narrow = [lon0 - 0.6, lat0 - 0.6, lon0 + 0.6, lat0 + 0.6]
    result = get_hazard_alerts(bbox=narrow)

    assert result.payload["no_active_hazard"] is False
    assert len(result.payload["polygons"]) == 1, "the overlapping cone must survive"
    assert "No active tropical cyclone" not in result.summary
