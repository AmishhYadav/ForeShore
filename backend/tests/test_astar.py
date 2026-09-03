"""Tests for ``foreshore.routing.astar`` — the real A* router.

CLAUDE.md is explicit about why this module exists at all: *"Routing uses A* over a
weighted grid ... Never LLM-generated waypoints. This is a credibility tripwire — a
fake router is instantly visible to an ISRO judge."* Three things are under test:

1. Synthetic-grid unit tests (fast, no data files) — the search never steps on an
   ``inf``-cost cell, reports what it routed around, and the three failure modes
   (``origin_blocked`` / ``destination_blocked`` / ``no_path_found``) behave exactly as
   specified rather than crashing.
2. Heuristic admissibility — ``h(cell) = haversine_nm(cell, destination) *
   field.meta["min_finite_cost"]`` must never overestimate. Run the same grid once with
   the real heuristic and once with ``h=0`` (Dijkstra); an admissible heuristic finds a
   path of the exact same total cost. A mismatch here means the heuristic is wrong, not
   that the test is too strict.
3. The flagship "does the router actually bend" integration test, on the real
   ``palk_bay_gom`` cost field in ``FORESHORE_MODE=fixture``: a straight line from
   Rameswaram crosses within the IMBL historic-waters hard buffer, and the real A* route
   must stay outside it at every single waypoint — verified independently via
   :meth:`~foreshore.store.vectors.VectorStore.nearest`, not by trusting the cost
   field's own internal bookkeeping.
"""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pytest
from foreshore.config import load_region, load_routing_config, load_vessels
from foreshore.routing import costfield as costfield_module
from foreshore.routing.astar import find_route
from foreshore.routing.costfield import CostField, build_cost_field
from foreshore.store.vectors import VectorStore

# --------------------------------------------------------------------------------------
# Synthetic-grid helpers
# --------------------------------------------------------------------------------------


def _blank_terms(n: int) -> dict[str, np.ndarray]:
    return {name: np.zeros((n, n)) for name in ("base", "hs", "wind", "current", "shallow", "steep", "imbl")}


def _make_field(cost: np.ndarray, blocked_reason: np.ndarray, *, min_finite_cost: float | None = None) -> CostField:
    """Hand-built :class:`CostField` for a synthetic grid — same shape contract
    ``build_cost_field`` produces, but with no data files or network involved.

    ``terms["base"]`` is set to match ``cost`` itself (zero where blocked, since those
    cells are never expanded and the value is never read) so a leg's
    ``cost_breakdown`` sums to the same number ``find_route``'s own ``g`` score used —
    real per-term bookkeeping, not a placeholder.
    """
    n = cost.shape[0]
    lats = np.linspace(9.00, 9.00 + 0.01 * (n - 1), n)
    lons = np.linspace(79.00, 79.00 + 0.01 * (n - 1), n)
    terms = _blank_terms(n)
    terms["base"] = np.where(np.isfinite(cost), cost, 0.0)
    finite = cost[np.isfinite(cost)]
    if min_finite_cost is None:
        min_finite_cost = float(finite.min()) if finite.size else 0.0
    meta = {
        "min_finite_cost": min_finite_cost,
        "weights": {}, "normalisers": {},
        "departure": "2026-09-01T00:00:00+00:00",
        "vessel_class": "small_motorised",
        "grid_shape": [n, n],
        "grid_deg": 0.01,
        "missing": [],
    }
    nan_grid = np.full((n, n), np.nan)
    return CostField(
        lats=lats, lons=lons, cost=cost, terms=terms, blocked_reason=blocked_reason,
        provenance=[], evidence=[], meta=meta,
        current_speed_kn=nan_grid, current_dir_deg=nan_grid.copy(),
    )


def _wall_field(n: int = 12, wall_col: int = 6, reason: str = "exclusion") -> CostField:
    """Uniform-cost ``n x n`` grid with a vertical wall of ``inf``-cost cells at
    ``wall_col``, open only at the very top and bottom rows — any west-to-east route
    must detour through one of those two gaps."""
    cost = np.ones((n, n))
    blocked = np.full((n, n), "", dtype=object)
    cost[1:n - 1, wall_col] = np.inf
    blocked[1:n - 1, wall_col] = reason
    return _make_field(cost, blocked)


def _textured_wall_field(n: int = 14, wall_col: int = 7) -> CostField:
    """Like :func:`_wall_field` but with non-uniform cost elsewhere (an expensive strip
    near the top gap, an expensive patch away from the wall), so an optimal path is not
    simply "closest to the straight line" — a meaningful case for the Dijkstra
    cross-check, not a vacuous one where every path happens to cost the same."""
    cost = np.ones((n, n))
    blocked = np.full((n, n), "", dtype=object)
    cost[1:n - 1, wall_col] = np.inf
    blocked[1:n - 1, wall_col] = "exclusion"
    cost[0, wall_col - 2:wall_col + 3] = 8.0       # make the top gap expensive to reach
    cost[5:9, 3:6] = 3.0                           # unrelated texture elsewhere
    return _make_field(cost, blocked)


def _leg_total_cost(route) -> float:
    return sum(sum(leg.cost_breakdown.values()) for leg in route.legs)


def _isolated_field(n: int = 20) -> tuple[CostField, tuple[float, float]]:
    """A grid with a solid ``inf`` block far larger than ``astar._SNAP_RADIUS_CELLS``
    (currently 4) *including* its own centre cell, so the centre point has no passable
    cell — itself or any neighbour — within the router's snap radius: a genuinely
    unreachable point, not a coastline-mask artifact that snapping should rescue.
    Returns the field and the centre cell's (lat, lon)."""
    cost = np.ones((n, n))
    blocked = np.full((n, n), "", dtype=object)
    cy, cx = n // 2, n // 2
    radius = 6  # comfortably beyond _SNAP_RADIUS_CELLS = 4
    for di in range(-radius, radius + 1):
        for dj in range(-radius, radius + 1):
            cost[cy + di, cx + dj] = np.inf
            blocked[cy + di, cx + dj] = "exclusion"
    field = _make_field(cost, blocked)
    return field, (field.lats[cy], field.lons[cx])


# --------------------------------------------------------------------------------------
# 1. Synthetic grid — feasible routing, avoidance, and the three failure modes.
# --------------------------------------------------------------------------------------


def test_astar_routes_around_a_wall_and_never_steps_on_a_blocked_cell():
    field = _wall_field(n=12, wall_col=6, reason="exclusion")
    origin = (field.lats[0], field.lons[0])
    destination = (field.lats[0], field.lons[-1])

    route = find_route(field, origin, destination, cruise_speed_kn=6.0)

    assert route.feasible is True
    assert route.failure_reason is None
    assert len(route.waypoints) >= 2

    for lat, lon in route.waypoints:
        i, j = field.index_of(lat, lon)
        assert math.isfinite(field.cost[i, j]), f"route stepped on a blocked cell at ({lat}, {lon})"

    assert "exclusion" in route.avoided
    assert math.isfinite(route.total_distance_nm) and route.total_distance_nm > 0
    assert math.isfinite(_leg_total_cost(route))
    # every leg accounts for real, positive haversine distance, not a placeholder
    for leg in route.legs:
        assert leg.distance_nm > 0


def test_astar_waypoints_are_only_cells_the_search_actually_reached():
    """Every waypoint must land exactly on a grid cell centre — never an interpolated
    or invented coordinate."""
    field = _wall_field(n=12, wall_col=6)
    origin = (field.lats[0], field.lons[0])
    destination = (field.lats[-1], field.lons[-1])

    route = find_route(field, origin, destination, cruise_speed_kn=6.0)
    assert route.feasible is True

    lat_set = set(np.round(field.lats, 8).tolist())
    lon_set = set(np.round(field.lons, 8).tolist())
    for lat, lon in route.waypoints:
        assert round(lat, 8) in lat_set
        assert round(lon, 8) in lon_set


def test_astar_origin_blocked_returns_origin_blocked_when_nothing_passable_in_snap_range():
    """A thin-wall blocked cell has passable neighbours one column over and gets snapped
    (see the dedicated snap test below) — that is deliberately no longer a failure. This
    test uses a point with no passable cell anywhere within the snap radius, so it must
    still fail exactly as before."""
    field, blocked_cell = _isolated_field()
    open_cell = (field.lats[0], field.lons[0])

    route = find_route(field, blocked_cell, open_cell, cruise_speed_kn=6.0)

    assert route.feasible is False
    assert route.failure_reason == "origin_blocked"
    assert route.waypoints == []
    assert route.legs == []


def test_astar_destination_blocked_returns_destination_blocked_when_nothing_passable_in_snap_range():
    field, blocked_cell = _isolated_field()
    open_cell = (field.lats[0], field.lons[0])

    route = find_route(field, open_cell, blocked_cell, cruise_speed_kn=6.0)

    assert route.feasible is False
    assert route.failure_reason == "destination_blocked"
    assert route.waypoints == []


def test_astar_snaps_a_blocked_origin_to_the_nearest_passable_cell():
    """A cell that is itself blocked, but has a passable cell within the snap radius (as
    a real harbour point on a coarse coastline mask does), must not fail — the router
    snaps to the nearest real, passable grid cell and routes from there."""
    field = _wall_field(n=12, wall_col=6)
    blocked_cell = (field.lats[5], field.lons[6])  # on the wall; col 5 and col 7 are open
    open_cell = (field.lats[0], field.lons[0])

    route = find_route(field, blocked_cell, open_cell, cruise_speed_kn=6.0)

    assert route.feasible is True
    assert route.failure_reason is None
    assert len(route.waypoints) >= 2
    # the snapped start is a real lattice cell within the snap radius of the blocked one
    start_lat, start_lon = route.waypoints[0]
    i, j = field.index_of(start_lat, start_lon)
    assert math.isfinite(field.cost[i, j])
    assert abs(i - 5) <= 4 and abs(j - 6) <= 4


def test_astar_no_path_found_when_goal_is_fully_enclosed():
    n = 12
    cost = np.ones((n, n))
    blocked = np.full((n, n), "", dtype=object)
    cy, cx = 6, 6
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            if di == 0 and dj == 0:
                continue
            cost[cy + di, cx + dj] = np.inf
            blocked[cy + di, cx + dj] = "exclusion"
    field = _make_field(cost, blocked)

    origin = (field.lats[0], field.lons[0])
    destination = (field.lats[cy], field.lons[cx])  # passable itself, but walled in on all 8 sides

    route = find_route(field, origin, destination, cruise_speed_kn=6.0)

    assert route.feasible is False
    assert route.failure_reason == "no_path_found"
    assert route.waypoints == []
    # the search still recorded what it ran into while it searched
    assert "exclusion" in route.avoided


def test_astar_start_equals_goal_is_a_single_waypoint_feasible_route():
    """A degenerate but legal case: origin and destination map to the same grid cell."""
    field = _wall_field(n=12, wall_col=6)
    point = (field.lats[2], field.lons[2])

    route = find_route(field, point, point, cruise_speed_kn=6.0)

    assert route.feasible is True
    assert len(route.waypoints) == 1
    assert route.legs == []
    assert route.total_distance_nm == 0.0


# --------------------------------------------------------------------------------------
# 2. Heuristic admissibility — real heuristic vs h=0 (Dijkstra) must agree on optimal cost.
# --------------------------------------------------------------------------------------


def test_heuristic_matches_dijkstra_on_a_uniform_grid():
    field = _wall_field(n=12, wall_col=6)
    origin = (field.lats[0], field.lons[0])
    destination = (field.lats[0], field.lons[-1])

    with_heuristic = find_route(field, origin, destination, cruise_speed_kn=6.0)

    field_dijkstra = _make_field(field.cost, field.blocked_reason, min_finite_cost=0.0)
    dijkstra = find_route(field_dijkstra, origin, destination, cruise_speed_kn=6.0)

    assert with_heuristic.feasible and dijkstra.feasible
    assert _leg_total_cost(with_heuristic) == pytest.approx(_leg_total_cost(dijkstra), rel=1e-9)
    assert with_heuristic.total_distance_nm == pytest.approx(dijkstra.total_distance_nm, rel=1e-9)


def test_heuristic_matches_dijkstra_on_a_textured_non_uniform_grid():
    """A grid where the geometrically-shortest path is *not* the cheapest one — a
    meaningful check that an admissible heuristic still finds the true cost optimum,
    not just any feasible path."""
    field = _textured_wall_field(n=14, wall_col=7)
    origin = (field.lats[0], field.lons[0])
    destination = (field.lats[-1], field.lons[-1])

    with_heuristic = find_route(field, origin, destination, cruise_speed_kn=6.0)

    field_dijkstra = _make_field(field.cost, field.blocked_reason, min_finite_cost=0.0)
    dijkstra = find_route(field_dijkstra, origin, destination, cruise_speed_kn=6.0)

    assert with_heuristic.feasible and dijkstra.feasible
    cost_h = _leg_total_cost(with_heuristic)
    cost_d = _leg_total_cost(dijkstra)
    assert cost_h == pytest.approx(cost_d, rel=1e-9), (
        f"heuristic path cost {cost_h} != Dijkstra optimal cost {cost_d} — "
        "the heuristic is inadmissible; fix find_route's heuristic, not this test"
    )
    # sanity: the textured grid actually has non-trivial structure (not every route the
    # same length), otherwise this test would pass vacuously
    assert cost_h > with_heuristic.total_distance_nm


# --------------------------------------------------------------------------------------
# 3. Flagship integration test — the real palk_bay_gom cost field actually bends around
#    the IMBL historic-waters hard exclusion buffer.
# --------------------------------------------------------------------------------------


def test_router_bends_around_the_imbl_hard_buffer_near_rameswaram():
    region = load_region("palk_bay_gom")
    routing_cfg = load_routing_config()
    vessel = load_vessels().get("small_motorised")
    hard_buffer_nm = routing_cfg.imbl["hard_buffer_nm"]
    assert hard_buffer_nm == pytest.approx(0.3)

    port = region.anchor_ports[0]
    assert port.name == "Rameswaram"

    field = build_cost_field(region_id="palk_bay_gom", vessel_class_id="small_motorised")

    # Rameswaram's own charted port coordinate is on a blocked ("land") cell at this
    # fixture's ~1 km coastline-mask resolution — the router's built-in origin-snapping
    # (see astar._nearest_passable_cell) resolves this to the nearest passable water
    # cell, so the literal port coordinate can be used directly here.
    origin = (port.lat, port.lon)
    # Due east of Rameswaram, on the far side of the IMBL 1974 historic-waters line
    # (line_id 1306) that runs roughly north-south through this stretch of the strait.
    destination = (port.lat, 79.75)

    store = VectorStore()

    # -- premise: the straight line genuinely violates the hard buffer -----------------
    straight_min_nm = min(
        store.nearest(
            "imbl_historic_waters",
            origin[0] + t * (destination[0] - origin[0]),
            origin[1] + t * (destination[1] - origin[1]),
            n=1,
        )[0].distance_nm
        for t in np.linspace(0.0, 1.0, 200)
    )
    assert straight_min_nm < hard_buffer_nm, (
        "test setup problem: the straight line from Rameswaram to the chosen "
        "destination never actually enters the IMBL hard buffer, so this is not "
        "exercising the 'does the router bend' behaviour it's meant to"
    )

    # -- the real A* route must stay outside the hard buffer at every waypoint ---------
    route = find_route(field, origin, destination, cruise_speed_kn=vessel.cruise_speed_kn)

    assert route.feasible is True, f"expected a feasible route, got failure_reason={route.failure_reason!r}"
    assert len(route.waypoints) >= 2
    assert "imbl" in route.avoided

    for lat, lon in route.waypoints:
        hits = store.nearest("imbl_historic_waters", lat, lon, n=1)
        assert hits, "VectorStore.nearest returned no result for the IMBL historic-waters layer"
        distance_nm = hits[0].distance_nm
        assert distance_nm >= hard_buffer_nm, (
            f"waypoint ({lat}, {lon}) is {distance_nm:.4f} nm from the IMBL historic-"
            f"waters line — inside the {hard_buffer_nm} nm hard exclusion buffer"
        )

    # the route is a genuine detour, not the (blocked) straight line
    assert route.total_distance_nm > route.direct_distance_nm


# --------------------------------------------------------------------------------------
# 4. Cost-field caching -- build_cost_field must not rebuild within the same
#    (region, vessel class, hour-bucket) key. This is the fix for the ~11 s-per-call
#    live-demo latency risk described in the task: a judge asking a second, nearby
#    routing question during the demo must not pay the full build cost again.
# --------------------------------------------------------------------------------------


def test_build_cost_field_reuses_the_same_field_within_one_hour_bucket(monkeypatch):
    """Two calls with the same region/vessel and departures fifteen minutes apart --
    still inside the same hour bucket -- must hit the cost-field cache: the expensive
    uncached builder runs exactly once, and the second call gets back the identical
    (by object identity) CostField rather than a freshly rebuilt equal one."""
    costfield_module._build_cost_field_cached.cache_clear()

    real_uncached = costfield_module._build_cost_field_uncached
    calls: list[int] = []

    def _counting_uncached(*args, **kwargs):
        calls.append(1)
        return real_uncached(*args, **kwargs)

    monkeypatch.setattr(costfield_module, "_build_cost_field_uncached", _counting_uncached)

    when_a = datetime(2026, 9, 1, 6, 5, 0)
    when_b = datetime(2026, 9, 1, 6, 47, 0)  # same hour bucket as when_a, different minute

    field1 = costfield_module.build_cost_field(
        region_id="palk_bay_gom", vessel_class_id="small_motorised", when=when_a,
    )
    field2 = costfield_module.build_cost_field(
        region_id="palk_bay_gom", vessel_class_id="small_motorised", when=when_b,
    )

    assert len(calls) == 1, (
        "the second call fell inside the same hour bucket as the first and must have "
        "been served from cache, not rebuilt from scratch"
    )
    assert field2 is field1

    # -- a different hour bucket is a genuine cache miss and rebuilds ------------------
    when_c = datetime(2026, 9, 1, 7, 5, 0)
    field3 = costfield_module.build_cost_field(
        region_id="palk_bay_gom", vessel_class_id="small_motorised", when=when_c,
    )
    assert len(calls) == 2
    assert field3 is not field1

    costfield_module._build_cost_field_cached.cache_clear()
