"""Tool 17 — which vessels are closest to a boundary, right now.

The shore console's analyst query box ships with the placeholder question *"Which
vessels are closest to the IMBL right now?"* — and until this tool existed, nothing in
the registry could see the fleet at all. ``check_geofences`` (tool 9) answers "am I near
a fence" for one position; this tool turns the same computation around and ranks the
*whole* tracked fleet against a boundary, which is what a shore-side fleet-view operator
actually asks, and what the problem statement's fleet-view capability requires.

Positions come from :func:`foreshore.push.vessels.current_fleet`, never a second,
independently-drifting simulation: if the push loop is running in this process, the
ranking uses exactly the positions the console map and the alert queue are already
showing (``fleet_source="push_loop"``); if no loop has started yet, a fresh deterministic
``default_fleet`` stands in (``fleet_source="cold_start"``) so the tool still answers
rather than failing an agent turn asked before the demo's background thread exists.

CLAUDE.md: there is no public real-time AIS feed for Indian small boats, so every vessel
here is simulated and every non-empty answer says so — on the wire, in the qualifiers,
and in the prose.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..config import load_region
from ..geofence.classes import GEOFENCE_CLASSES, format_eta, title_for
from ..geofence.engine import shared_engine
from ..models import Observation, ToolResult
from ..push.vessels import current_fleet
from .registry import registry

#: The literal alias the console's own placeholder question types — "the IMBL" — meaning
#: both hard-legal boundary classes together. It is a query-time convenience, never a
#: merged third geofence class: every ranked row still carries its own distinct
#: ``geofence_class``.
_IMBL_ALIAS = "IMBL"
_IMBL_CLASSES: tuple[str, ...] = ("IMBL_HISTORIC_WATERS", "IMBL_MARITIME_BOUNDARY")

#: Spoken-prose stand-ins for the alert-level enum. Never the raw code in a summary.
_LEVEL_WORDING: dict[str, str] = {
    "BREACH": "already inside",
    "CRITICAL": "critical range",
    "WARN": "warning range",
    "INFO": "clear",
}


def _resolve_classes(geofence_class: str | None) -> tuple[list[str] | None, bool]:
    """Case-insensitive resolution against :data:`GEOFENCE_CLASSES`, plus the ``"IMBL"``
    alias.

    Returns ``(resolved, ok)``. ``resolved`` is ``None`` for "every class"
    (omitted/``None`` input), a one-element list for a single matched class, or the
    two-element IMBL alias. ``ok=False`` means the name matched nothing known — not an
    error, just something the caller reports as ``missing``.
    """
    if geofence_class is None:
        return None, True
    normalised = geofence_class.strip().upper()
    if normalised == _IMBL_ALIAS:
        return list(_IMBL_CLASSES), True
    for gc in GEOFENCE_CLASSES:
        if gc.upper() == normalised:
            return [gc], True
    return None, False


def _boundary_phrase(present_classes: Sequence[str] | None) -> str:
    """Human phrase naming the boundary/boundaries in play, in the classes' canonical
    (hardest-consequence-first) order — never a raw code."""
    ordered = [gc for gc in GEOFENCE_CLASSES if gc in (present_classes or ())]
    if not ordered:
        return "a tracked geofence boundary"
    titles = [title_for(gc, "en") for gc in ordered]
    if len(titles) == 1:
        return f"the {titles[0]}"
    if len(titles) == 2:
        return f"the {titles[0]} and the {titles[1]}"
    return "the " + ", the ".join(titles[:-1]) + ", and the " + titles[-1]


def _vessel_clause(row: dict[str, Any]) -> str:
    wording = _LEVEL_WORDING.get(row["level"], "clear")
    clause = f"{row['name']} at {row['distance_nm']:.2f} nm ({wording})"
    # No closing ETA for a vessel that is already inside. The engine still reports one —
    # the distance to the nearest edge is real — but "already inside, closing in 2 min"
    # is a contradiction to read, and the time to an edge you are within is not something
    # an operator acts on. The breach is the fact.
    if row["eta_seconds"] is not None and not row.get("inside"):
        clause += f", closing in {format_eta(row['eta_seconds'], 'en')}"
    return clause


@registry.tool(
    name="find_vessels_near_boundary",
    number=17,
    description=(
        "Rank the tracked fleet by distance to a geofence class — which vessels are "
        "closest to the India-Sri Lanka maritime boundary, to a marine national park, "
        "or to any hazard exclusion zone, with bearing and closing ETA for each. "
        "Positions are simulated: there is no public real-time AIS feed for Indian "
        "small boats, and this tool says so on every answer."
    ),
    schema={
        "type": "object",
        "properties": {
            "geofence_class": {
                "type": "string",
                "description": (
                    "Which boundary class to measure against: IMBL_HISTORIC_WATERS, "
                    "IMBL_MARITIME_BOUNDARY, IMBL (both boundary classes), MPA, "
                    "ECO_SENSITIVE, USER_DEFINED, HAZARD_EXCLUSION. Omit for every class."
                ),
            },
            "limit": {"type": "integer", "description": "How many vessels to rank. Default 5."},
            "max_nm": {"type": "number", "description": "Ignore fences further than this."},
        },
        "required": [],
    },
    specialists=("GeospatialReasoning", "ReportingAgent", "VisualizationAgent"),
    reads_sources=("simulated_fleet", "marineregions_eez_boundaries"),
    cost="fast",
)
def find_vessels_near_boundary(
    geofence_class: str | None = None,
    limit: int = 5,
    max_nm: float | None = None,
) -> ToolResult:
    """Rank the tracked fleet by distance to the requested geofence class(es).

    Never raises: any failure degrades to a failed :class:`ToolResult`. An unrecognised
    class name and an empty tracked fleet are both reported via ``missing``, not as
    errors — they are the honest answer to "rank against a boundary that does not exist"
    and "rank a fleet that is not being tracked", not a system fault.
    """
    try:
        resolved, class_ok = _resolve_classes(geofence_class)
        if not class_ok:
            known = ", ".join(title_for(gc, "en") for gc in GEOFENCE_CLASSES)
            return ToolResult(
                tool="find_vessels_near_boundary",
                ok=True,
                missing=["geofence_class"],
                summary=(
                    f"'{geofence_class}' is not a recognised geofence class. Known "
                    f"classes: {known}. Use 'IMBL' to mean both IMBL boundary classes "
                    "together."
                ),
                payload={
                    "vessels": [],
                    "geofence_class": None,
                    "fleet_source": None,
                    "total_tracked": 0,
                    "is_simulated": True,
                },
            )

        region = load_region()
        vessels, fleet_source = current_fleet(region)

        if not vessels:
            return ToolResult(
                tool="find_vessels_near_boundary",
                ok=True,
                missing=["fleet_positions"],
                summary=(
                    "No vessel positions are being tracked, so no vessel can be ranked "
                    "against a boundary."
                ),
                payload={
                    "vessels": [],
                    "fleet_source": fleet_source,
                    "geofence_class": resolved,
                    "total_tracked": 0,
                    "is_simulated": True,
                },
            )

        engine = shared_engine(region)
        clamped_limit = max(1, min(50, limit))

        # Per vessel: the single nearest matching fence, across every checked layer.
        hit_pairs: list[tuple[Any, Any]] = []  # (vessel, GeofenceProximity)
        for v in vessels:
            hits = engine.check(
                v.lat, v.lon, v.heading_deg, v.speed_kn,
                classes=resolved, include_info=True, max_nm=max_nm,
            )
            if not hits:
                continue
            nearest = min(hits, key=lambda p: p.distance_nm)
            hit_pairs.append((v, nearest))

        hit_pairs.sort(key=lambda pair: pair[1].distance_nm)
        hit_pairs = hit_pairs[:clamped_limit]

        observations: list[Observation] = []
        vessel_rows: list[dict[str, Any]] = []
        for v, prox in hit_pairs:
            observations.append(
                Observation(
                    variable="vessel_boundary_distance",
                    value=round(prox.distance_nm, 3),
                    unit="nm",
                    lat=v.lat,
                    lon=v.lon,
                    valid_time=v.updated_at,
                    provenance=prox.provenance,
                    qualifiers={
                        "vessel_id": v.vessel_id,
                        "vessel_name": v.name,
                        "is_simulated": True,
                        "fleet_source": fleet_source,
                        "geofence_class": prox.geofence_class,
                        "geofence_name": prox.name,
                        "level": prox.level,
                        "bearing_deg": prox.bearing_deg,
                        "eta_seconds": prox.eta_seconds,
                        "inside": prox.inside,
                        "speed_kn": v.speed_kn,
                        "heading_deg": v.heading_deg,
                        "home_port": v.home_port,
                        "crew": v.crew,
                    },
                )
            )
            vessel_rows.append(
                {
                    "vessel_id": v.vessel_id,
                    "name": v.name,
                    "lat": v.lat,
                    "lon": v.lon,
                    "heading_deg": v.heading_deg,
                    "speed_kn": v.speed_kn,
                    "home_port": v.home_port,
                    "crew": v.crew,
                    "is_simulated": v.is_simulated,
                    "distance_nm": round(prox.distance_nm, 3),
                    "bearing_deg": prox.bearing_deg,
                    "eta_seconds": prox.eta_seconds,
                    "inside": prox.inside,
                    "level": prox.level,
                    "geofence_class": prox.geofence_class,
                    "geofence_title": title_for(prox.geofence_class, "en"),
                    "geofence_name": prox.name,
                    "closest_lat": prox.closest_lat,
                    "closest_lon": prox.closest_lon,
                }
            )

        present_classes = sorted({row["geofence_class"] for row in vessel_rows})
        # Fall back to what was *asked* about when nothing was found: "none are near the
        # 1974 line" is an answer, "none are near a tracked geofence boundary" is not.
        boundary_phrase = _boundary_phrase(present_classes or resolved)

        if vessel_rows:
            # A ranking, not an in-range filter: `include_info=True` deliberately keeps
            # vessels that are comfortably clear, because "which are closest" wants an
            # ordered list even on a quiet day. Saying "within range" of a boat 12 nm
            # off and marked clear in the same sentence would be a contradiction.
            counted = (
                f"Closest {len(vessel_rows)} of {len(vessels)} tracked vessels"
                if len(vessel_rows) < len(vessels)
                else f"All {len(vessels)} tracked vessels"
            )
            summary = (
                f"{counted}, ranked by distance to {boundary_phrase}: "
                + "; ".join(_vessel_clause(row) for row in vessel_rows)
                + ". All positions are simulated — there is no public real-time AIS "
                "feed for Indian small boats."
            )
        else:
            summary = (
                f"None of the {len(vessels)} tracked vessels could be measured against "
                f"{boundary_phrase}. All positions are simulated — there is no public "
                "real-time AIS feed for Indian small boats."
            )

        payload = {
            "vessels": vessel_rows,
            "geofence_class": resolved,
            "fleet_source": fleet_source,
            "total_tracked": len(vessels),
            "is_simulated": True,
        }

        return ToolResult(
            tool="find_vessels_near_boundary",
            ok=True,
            observations=observations,
            payload=payload,
            summary=summary,
        )
    except Exception as exc:  # noqa: BLE001 — a ranking bug must abstain, not crash the loop
        return ToolResult(
            tool="find_vessels_near_boundary",
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            # This summary is spliced verbatim into the answer a reader sees, so it is a
            # sentence, not a traceback. The class and message stay on `error` for the
            # trace inspector.
            summary=(
                "The fleet could not be ranked against a boundary on this run, so no "
                "vessel position is reported here."
            ),
            missing=["find_vessels_near_boundary"],
        )


__all__ = ["find_vessels_near_boundary"]
