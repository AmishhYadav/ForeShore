"""Tool 11 -- ``plan_route``: A* passage planning over the weighted routing cost field.

PLAN.md / CLAUDE.md are explicit that this tool must never hand back an LLM-guessed
path: ``routing/costfield.py`` builds a real, sourced, weighted grid (wave height,
wind, current, depth, wave steepness, boundary proximity) and ``routing/astar.py`` runs
a real 8-connected A* search over it. This module's only job is to wire the two
together behind the tool contract, translate the outcome into a :class:`ToolResult`,
and make sure every number it surfaces carries provenance.

**Graceful degradation, mirroring ``nearest_harbour``'s pattern.** A route that cannot
be planned is a stated outcome, never a crash: if the cost field itself cannot be built,
or if A* returns ``feasible=False`` (blocked origin/destination, or no passable path in
the grid), this tool still returns ``ok=True, partial=True`` with the reason named in
``summary`` -- so a caller relying on this for a route always gets an honest answer, not
an exception.
"""

from __future__ import annotations

from datetime import datetime

from ..config import load_vessels
from ..models import Observation, Provenance, Route, ToolResult, utcnow
from ..routing.astar import find_route
from ..routing.costfield import CostField, build_cost_field
from .registry import registry

#: Source adapters ``routing/costfield.py`` actually attaches to ``field.provenance`` --
#: read off its own ``provenance.append(...)`` calls, not guessed: the INCOIS OSF
#: wave/wind/current products it fetches, the Marine Regions IMBL lines it measures
#: proximity against, and the INCOIS bathymetry contours it samples depth from.
_READS_SOURCES: tuple[str, ...] = (
    "incois_osf_wave", "incois_osf_winds", "incois_osf_currents",
    "marine_regions_imbl", "incois_wfs",
)

_FAILURE_TEXT: dict[str, str] = {
    "origin_blocked": (
        "the origin sits inside a hard-excluded cell -- land, the IMBL hard buffer, a "
        "dynamic hazard exclusion, or water shallower than this vessel's draft"
    ),
    "destination_blocked": (
        "the destination sits inside a hard-excluded cell -- land, the IMBL hard "
        "buffer, a dynamic hazard exclusion, or water shallower than this vessel's draft"
    ),
    "no_path_found": (
        "no passable route exists between origin and destination anywhere in the "
        "routing grid -- every path the search tried ran into a hard exclusion"
    ),
}


def _parse_departure(value: str | None) -> tuple[datetime | None, str | None]:
    """Tolerant ISO-8601 parse. ``None``/unparsable both fall back to "now" inside
    ``build_cost_field`` -- never silently assumed to be a specific instant here."""
    if value is None:
        return None, None
    s = value.strip()
    if not s:
        return None, None
    try:
        s2 = f"{s[:-1]}+00:00" if s.endswith("Z") else s
        return datetime.fromisoformat(s2), None
    except ValueError:
        return None, f"could not parse departure={value!r}; used the current time instead"


def _route_provenance(route: Route, field: CostField) -> Provenance:
    """One derived Provenance for the route-level numbers (distance, ETA, detour) --
    the route is real arithmetic over a real grid, not a single external record, so this
    names every underlying source the cost field actually carried rather than pointing
    at one of them arbitrarily."""
    sources = sorted({p.source_id for p in field.provenance})
    grid_shape = field.meta.get("grid_shape")
    notes = (
        f"A* route computed deterministically over the routing cost field "
        f"({grid_shape[0]}x{grid_shape[1]} cells at {field.meta.get('grid_deg')} deg -- "
        f"~{field.meta.get('grid_deg', 0) * 111_000:.0f} m per cell); built from: "
        + (", ".join(sources) if sources else "no live cost-field source records were available for this call")
        + "."
    )
    if field.meta.get("missing"):
        notes += f" Missing inputs (defaulted to zero, never guessed): {', '.join(field.meta['missing'])}."
    return Provenance(
        source_id="foreshore_routing",
        source_name="FORESHORE A* router over the weighted routing cost field",
        authority="derived",
        url="local://routing/plan_route",
        acquired_at=utcnow(),
        issued_at=route.departure or utcnow(),
        is_derived=True,
        notes=notes,
    )


def _route_observations(route: Route, field: CostField, origin: tuple[float, float]) -> list[Observation]:
    """``field.evidence`` (the grid-level wave/wind/current summaries the route was
    computed against) plus one Observation per route-level number actually quoted in the
    summary/UI -- so a synthesised sentence naming a distance or ETA always has a real
    Observation behind it (invariant 3), not just a structured payload."""
    observations = list(field.evidence)
    if not route.feasible:
        return observations
    prov = _route_provenance(route, field)
    lat, lon = origin
    valid_time = route.departure or utcnow()
    qualifiers = {"legs": len(route.legs), "avoided": route.avoided}
    for variable, value, unit in (
        ("route_total_distance", route.total_distance_nm, "nm"),
        ("route_direct_distance", route.direct_distance_nm, "nm"),
        ("route_detour", route.detour_pct, "%"),
        ("route_eta", route.total_eta_seconds, "s"),
    ):
        observations.append(Observation(
            variable=variable, value=round(float(value), 3), unit=unit, lat=lat, lon=lon,
            valid_time=valid_time, provenance=prov, qualifiers=qualifiers,
        ))
    return observations


@registry.tool(
    name="plan_route",
    number=11,
    description=(
        "Plan a passage between two points with A* over the weighted routing cost "
        "field (wave height, wind, current, depth, wave steepness and boundary "
        "proximity, all sourced -- never LLM-guessed). Returns real waypoints, "
        "per-leg distance/bearing/ETA, a per-term cost breakdown explaining why the "
        "route bends, and the list of hazards it actually routed around. A route that "
        "cannot be planned (blocked origin/destination, or no passable path) is "
        "returned as a stated outcome, not an error -- see 'partial' and 'summary'."
    ),
    schema={
        "type": "object",
        "properties": {
            "origin": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 2,
                "maxItems": 2,
                "description": "Origin position [lat, lon], decimal degrees (EPSG:4326).",
            },
            "destination": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 2,
                "maxItems": 2,
                "description": "Destination position [lat, lon], decimal degrees (EPSG:4326).",
            },
            "departure": {
                "type": ["string", "null"],
                "description": (
                    "Optional ISO-8601 departure time. Selects which cost-field "
                    "wave/wind/current grids are sampled. Omit or null for now."
                ),
            },
            "vessel_class": {
                "type": ["string", "null"],
                "description": (
                    "Optional vessel class id from config/vessels.yaml (e.g. "
                    "'small_motorised'). Omit or null for the catalogue's default class."
                ),
            },
        },
        "required": ["origin", "destination"],
    },
    specialists=("RoutingAgent",),
    reads_sources=_READS_SOURCES,
    cost="slow",
)
def plan_route(
    origin: list[float], destination: list[float],
    departure: str | None = None, vessel_class: str | None = None,
) -> ToolResult:
    """Build the routing cost field and run A* over it. Never invents a waypoint: every
    point in the returned route is a grid cell :func:`~foreshore.routing.astar.find_route`
    actually reached.

    Never raises: a cost field that cannot be built, or a search that finds no passable
    path, both degrade to ``ok=True, partial=True`` with the reason named in ``summary``
    -- a failed route is a designed outcome, not a crash.
    """
    when_dt, when_note = _parse_departure(departure)
    vessels = load_vessels()
    vessel = vessels.get(vessel_class)

    try:
        field = build_cost_field(when=when_dt, vessel_class_id=vessel.class_id)
    except Exception as exc:  # noqa: BLE001 -- an unbuildable cost field is an abstention
        return ToolResult(
            tool="plan_route",
            ok=True,
            partial=True,
            missing=["cost_field"],
            summary=(
                "No route could be planned: the routing cost field could not be built "
                f"({type(exc).__name__}: {exc}). Abstaining rather than guessing a path."
            ),
            payload={"route": None},
        )

    origin_t = (float(origin[0]), float(origin[1]))
    destination_t = (float(destination[0]), float(destination[1]))

    route = find_route(field, origin_t, destination_t, cruise_speed_kn=vessel.cruise_speed_kn)
    observations = _route_observations(route, field, origin_t)
    missing = list(field.meta.get("missing", []))
    when_bit = f" ({when_note})" if when_note else ""

    if not route.feasible:
        reason_text = _FAILURE_TEXT.get(route.failure_reason or "", route.failure_reason or "unknown")
        return ToolResult(
            tool="plan_route",
            ok=True,
            partial=True,
            missing=["route", *missing],
            observations=observations,
            payload={"route": route},
            summary=f"No route could be planned: {reason_text}.{when_bit}",
        )

    missing_bit = (
        f" Cost-field inputs unavailable this call (defaulted to zero, never treated as "
        f"safe): {', '.join(missing)}." if missing else ""
    )
    avoided_bit = f" Routed around: {', '.join(route.avoided)}." if route.avoided else ""
    summary = (
        f"Route planned: {route.total_distance_nm:.1f} nm over {len(route.legs)} leg(s), "
        f"ETA {route.total_eta_seconds / 3600.0:.1f} h at {vessel.cruise_speed_kn:.1f} kn "
        f"cruise ({route.detour_pct:.0f}% longer than the {route.direct_distance_nm:.1f} nm "
        f"direct line)." + avoided_bit + when_bit + missing_bit
    )

    return ToolResult(
        tool="plan_route",
        ok=True,
        partial=bool(missing),
        missing=missing,
        observations=observations,
        payload={"route": route},
        summary=summary,
    )


__all__ = ["plan_route"]
