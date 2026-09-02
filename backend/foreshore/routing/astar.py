"""A* search over a :class:`~foreshore.routing.costfield.CostField`.

PLAN.md / CLAUDE.md are explicit about why this module exists at all: *"Routing uses
A* over a weighted grid ... Never LLM-generated waypoints. This is a credibility
tripwire — a fake router is instantly visible to an ISRO judge."* Every waypoint this
module returns is a grid cell the search actually reached; every leg cost is
:meth:`CostField.breakdown_at` evaluated on that leg's own bearing. No smoothing, no
interpolation, no invented coordinate.

**Edge-cost model.** Moving from one cell centre to an adjacent cell centre costs
``field.cost[i, j] * haversine_nm(from, to)`` — the destination cell's per-nautical-mile
risk rate, times the true physical distance of that step. A diagonal step already costs
more than an orthogonal one simply because it covers more nm under
:func:`~foreshore.models.haversine_nm`; no separate sqrt(2) factor is layered on top of
that (doing so would double-count the very thing the real distance calculation already
captures).

**Heuristic.** ``h(cell) = haversine_nm(cell, destination) * field.meta["min_finite_cost"]``.
This is admissible: no real path can be physically shorter than the great-circle
distance remaining to the goal, and no passable cell anywhere in the field costs less
per nautical mile than ``min_finite_cost`` (the minimum finite value ``build_cost_field``
recorded over the whole grid) — so no real path from here to the goal can cost less than
``remaining_nm * min_finite_cost``. The heuristic is therefore never an overestimate,
which is exactly what admissibility requires for A* to still return the optimal path.
Cell size never enters the formula: under this per-nm edge-cost model (not a per-cell
one), scaling by cell size would make the heuristic overestimate on some grids and stop
being admissible.

**Origin/destination snapping.** A real anchor-port coordinate sits right at the
waterline; the ~1 km Natural Earth coastline mask `build_cost_field` uses routinely
classifies that exact point as land (verified: Rameswaram's charted point needs 2 grid
cells of search to reach open water, Nagapattinam needs 1). Failing every route request
that starts from a named port would be wrong, not conservative, so a blocked origin or
destination is snapped to the nearest passable cell within a small bounded radius before
the search runs. The snapped cell is still a real grid cell the field already carries a
cost for — nothing is invented. Only when no passable cell exists within that radius is
the point genuinely unreachable, and that still returns ``origin_blocked`` /
``destination_blocked`` exactly as before.
"""

from __future__ import annotations

import heapq
import itertools
import math
from datetime import datetime
from typing import Any

from ..models import Route, RouteLeg, bearing_deg, haversine_nm
from .costfield import CostField

#: 8-connected neighbourhood: N, S, E, W, NE, NW, SE, SW as (di, dj) offsets.
_NEIGHBOURS: tuple[tuple[int, int], ...] = (
    (-1, 0), (1, 0), (0, -1), (0, 1),
    (-1, -1), (-1, 1), (1, -1), (1, 1),
)


def _parse_iso(value: Any) -> datetime | None:
    """Tolerant ISO-8601 parse of ``field.meta["departure"]``. Never raises: a
    malformed or missing timestamp degrades ``Route.departure`` to ``None`` rather than
    crashing a route that otherwise resolved cleanly."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


#: Snap radius for a blocked origin/destination, in grid cells. Empirically, Rameswaram's
#: charted port point needs 2 cells to reach open water and Nagapattinam needs 1 — this
#: leaves a 2x margin without letting a genuinely inland point silently "snap" out to sea.
_SNAP_RADIUS_CELLS = 4


def _nearest_passable_cell(
    field: CostField, lat: float, lon: float, *, max_radius_cells: int = _SNAP_RADIUS_CELLS
) -> tuple[int, int] | None:
    """The cell containing ``(lat, lon)``, or the nearest passable cell within
    ``max_radius_cells`` Chebyshev rings if that exact cell is blocked. ``None`` if
    nothing passable is found in range — a genuinely unreachable point, not a coastline-
    mask artifact."""
    i0, j0 = field.index_of(lat, lon)
    if not math.isinf(float(field.cost[i0, j0])):
        return (i0, j0)
    nlat, nlon = field.cost.shape
    for r in range(1, max_radius_cells + 1):
        ring: list[tuple[int, int]] = []
        for di in range(-r, r + 1):
            for dj in range(-r, r + 1):
                if max(abs(di), abs(dj)) != r:
                    continue
                ni, nj = i0 + di, j0 + dj
                if 0 <= ni < nlat and 0 <= nj < nlon and not math.isinf(float(field.cost[ni, nj])):
                    ring.append((ni, nj))
        if ring:
            return min(ring, key=lambda idx: haversine_nm(lat, lon, *field.latlon_of(*idx)))
    return None


def _empty_route(
    *, direct_distance_nm: float, failure_reason: str, avoided: set[str],
    evidence: list, departure: datetime | None, vessel_class: str | None,
) -> Route:
    return Route(
        waypoints=[],
        legs=[],
        total_distance_nm=0.0,
        total_eta_seconds=0.0,
        direct_distance_nm=direct_distance_nm,
        cost_breakdown={},
        evidence=list(evidence),
        avoided=sorted(avoided),
        feasible=False,
        failure_reason=failure_reason,
        departure=departure,
        vessel_class=vessel_class,
    )


def find_route(
    field: CostField,
    origin: tuple[float, float],
    destination: tuple[float, float],
    *,
    cruise_speed_kn: float,
) -> Route:
    """8-connected A* over ``field.cost``, origin and destination given as (lat, lon).

    ``departure`` and ``vessel_class`` on the returned :class:`Route` come from
    ``field.meta`` — ``build_cost_field`` already stamped both there from its own call
    args, so they are read back rather than re-supplied (and re-invented) here.
    """
    origin_lat, origin_lon = origin
    dest_lat, dest_lon = destination
    direct_distance_nm = haversine_nm(origin_lat, origin_lon, dest_lat, dest_lon)
    departure = _parse_iso(field.meta.get("departure"))
    vessel_class = field.meta.get("vessel_class")

    start = _nearest_passable_cell(field, origin_lat, origin_lon)
    if start is None:
        return _empty_route(
            direct_distance_nm=direct_distance_nm, failure_reason="origin_blocked",
            avoided=set(), evidence=field.evidence, departure=departure, vessel_class=vessel_class,
        )
    goal = _nearest_passable_cell(field, dest_lat, dest_lon)
    if goal is None:
        return _empty_route(
            direct_distance_nm=direct_distance_nm, failure_reason="destination_blocked",
            avoided=set(), evidence=field.evidence, departure=departure, vessel_class=vessel_class,
        )

    nlat, nlon = field.cost.shape
    min_finite_cost = float(field.meta["min_finite_cost"])

    def heuristic(i: int, j: int) -> float:
        lat, lon = field.latlon_of(i, j)
        # Admissible — see module docstring for the proof: no passable cell costs less
        # than min_finite_cost per nm, and no path is shorter than the great-circle
        # remaining distance, so h never overestimates the true remaining cost.
        return haversine_nm(lat, lon, dest_lat, dest_lon) * min_finite_cost

    g_score: dict[tuple[int, int], float] = {start: 0.0}
    came_from: dict[tuple[int, int], tuple[int, int]] = {}
    closed: set[tuple[int, int]] = set()
    avoided_reasons: set[str] = set()

    counter = itertools.count()
    open_heap: list[tuple[float, int, tuple[int, int]]] = [
        (heuristic(*start), next(counter), start)
    ]

    reached = False
    while open_heap:
        _f, _, current = heapq.heappop(open_heap)
        if current in closed:
            continue
        closed.add(current)
        if current == goal:
            reached = True
            break

        ci, cj = current
        current_g = g_score[current]
        from_lat, from_lon = field.latlon_of(ci, cj)
        for di, dj in _NEIGHBOURS:
            ni, nj = ci + di, cj + dj
            if ni < 0 or ni >= nlat or nj < 0 or nj >= nlon:
                continue
            neighbour = (ni, nj)
            if neighbour in closed:
                continue
            ncost = float(field.cost[ni, nj])
            if math.isinf(ncost):
                reason = field.blocked_reason[ni, nj]
                if reason:
                    avoided_reasons.add(str(reason))
                continue

            to_lat, to_lon = field.latlon_of(ni, nj)
            step_nm = haversine_nm(from_lat, from_lon, to_lat, to_lon)
            tentative_g = current_g + ncost * step_nm

            if tentative_g < g_score.get(neighbour, math.inf):
                g_score[neighbour] = tentative_g
                came_from[neighbour] = current
                f_score = tentative_g + heuristic(ni, nj)
                heapq.heappush(open_heap, (f_score, next(counter), neighbour))

    if not reached:
        return _empty_route(
            direct_distance_nm=direct_distance_nm, failure_reason="no_path_found",
            avoided=avoided_reasons, evidence=field.evidence, departure=departure,
            vessel_class=vessel_class,
        )

    # -- reconstruct the path the search actually walked, grid index by grid index -----
    path_idx: list[tuple[int, int]] = [goal]
    node = goal
    while node != start:
        node = came_from[node]
        path_idx.append(node)
    path_idx.reverse()

    # -- to lat/lon, collapsing only literal consecutive duplicates --------------------
    dedup_idx: list[tuple[int, int]] = []
    waypoints: list[tuple[float, float]] = []
    for idx in path_idx:
        latlon = field.latlon_of(*idx)
        if waypoints and waypoints[-1] == latlon:
            continue
        dedup_idx.append(idx)
        waypoints.append(latlon)

    legs: list[RouteLeg] = []
    total_distance_nm = 0.0
    total_eta_seconds = 0.0
    cost_breakdown: dict[str, float] = {}

    for k in range(len(dedup_idx) - 1):
        i_to, j_to = dedup_idx[k + 1]
        from_lat, from_lon = waypoints[k]
        to_lat, to_lon = waypoints[k + 1]
        distance_nm = haversine_nm(from_lat, from_lon, to_lat, to_lon)
        leg_bearing = bearing_deg(from_lat, from_lon, to_lat, to_lon)
        eta_seconds = distance_nm / cruise_speed_kn * 3600.0
        breakdown = field.breakdown_at(i_to, j_to, leg_bearing)

        legs.append(RouteLeg(
            from_lat=from_lat, from_lon=from_lon, to_lat=to_lat, to_lon=to_lon,
            distance_nm=distance_nm, bearing_deg=leg_bearing, eta_seconds=eta_seconds,
            cost_breakdown=breakdown,
        ))
        total_distance_nm += distance_nm
        total_eta_seconds += eta_seconds
        for term, value in breakdown.items():
            cost_breakdown[term] = cost_breakdown.get(term, 0.0) + value

    return Route(
        waypoints=waypoints,
        legs=legs,
        total_distance_nm=total_distance_nm,
        total_eta_seconds=total_eta_seconds,
        direct_distance_nm=direct_distance_nm,
        cost_breakdown=cost_breakdown,
        evidence=list(field.evidence),
        avoided=sorted(avoided_reasons),
        feasible=True,
        failure_reason=None,
        departure=departure,
        vessel_class=vessel_class,
    )


__all__ = ["find_route"]
