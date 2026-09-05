"""The request path and the push path must see the same fences.

Dynamic hazard fences (cyclone cones, wind-radii polygons, high-wave cells) live in memory
on a :class:`GeofenceEngine` instance, and the push loop is the only thing that refreshes
them. While `tools/geofence_tools.py` held a *separate* instance, a vessel sitting inside
an active cyclone exclusion zone got `proximities: []` from `POST /api/geofence/check` and
from the agent's own tool call — indistinguishable from "no hazard nearby" — while the push
loop in the same process correctly flagged BREACH for the tracked fleet at that position.

CLAUDE.md invariant 5 keeps the geofence classes distinct; this file keeps them *reachable*
from both paths.
"""

from __future__ import annotations

import pytest

from foreshore.geofence.engine import reset_shared_engine, shared_engine
from foreshore.push.loop import PushLoop
from foreshore.tools import geofence_tools


#: A polygon around the Palk Bay demo centre, as a GDACS-shaped hazard feature.
HAZARD_FEATURE = {
    "type": "Feature",
    "properties": {"hazard_class": "cyclone_cone", "event_name": "TEST CYCLONE"},
    "geometry": {
        "type": "Polygon",
        "coordinates": [[[79.0, 9.0], [79.6, 9.0], [79.6, 9.6], [79.0, 9.6], [79.0, 9.0]]],
    },
}

INSIDE_LAT, INSIDE_LON = 9.3, 79.3


@pytest.fixture(autouse=True)
def _clean_shared_engine():
    """No dynamic fence may leak between tests — they are process-global state."""
    reset_shared_engine()
    yield
    reset_shared_engine()


def test_push_loop_and_the_tool_share_one_engine(region):
    loop = PushLoop(region=region, tick_seconds=1)
    assert loop.engine is geofence_tools._engine()
    assert loop.engine is shared_engine()


def test_a_hazard_the_push_loop_knows_about_is_visible_to_the_request_path(region):
    loop = PushLoop(region=region, tick_seconds=1)
    loop.engine.dynamic_from_features([HAZARD_FEATURE])

    result = geofence_tools.check_geofences(lat=INSIDE_LAT, lon=INSIDE_LON)
    classes = {p.get("geofence_class") for p in result.payload.get("proximities", [])}

    assert "HAZARD_EXCLUSION" in classes, (
        "a vessel inside an active cyclone exclusion zone must not be told the coast is "
        "clear by the request path"
    )


def test_clearing_the_hazard_clears_it_for_both(region):
    loop = PushLoop(region=region, tick_seconds=1)
    loop.engine.dynamic_from_features([HAZARD_FEATURE])
    loop.engine.clear_dynamic()

    result = geofence_tools.check_geofences(lat=INSIDE_LAT, lon=INSIDE_LON)
    classes = {p.get("geofence_class") for p in result.payload.get("proximities", [])}

    assert "HAZARD_EXCLUSION" not in classes


def test_the_shared_engine_is_rebuilt_when_the_region_changes(region):
    first = shared_engine(region)
    first.dynamic_from_features([HAZARD_FEATURE])
    assert first.dynamic

    from foreshore.config import load_region

    other = load_region("gujarat_sir_creek")
    second = shared_engine(other)

    assert second is not first
    assert second.dynamic == [], "fences from the previous region must not survive a swap"
