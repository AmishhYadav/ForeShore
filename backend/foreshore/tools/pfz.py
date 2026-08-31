"""Tool 7 — the official INCOIS Potential Fishing Zone (PFZ) advisory line.

This is deliberately narrow: it surfaces the government-issued PFZ line and nothing
else. Tool 8 (a FORESHORE-derived indicative PFZ product, for days INCOIS has not
published one) lives elsewhere and is intentionally not implemented here — mixing the
two in one module would make it too easy to accidentally hand back a derived product
under an "official" label, which is exactly the failure mode this file exists to avoid.
"""

from __future__ import annotations

from typing import Any

from ..models import ToolResult
from .registry import latlon_schema, registry


@registry.tool(
    name="find_nearest_pfz",
    number=7,
    description=(
        "Distance and bearing from a position to the nearest OFFICIAL INCOIS Potential "
        "Fishing Zone (PFZ) advisory line (PFZ_Automation:pfzlines). This is the "
        "government-issued PFZ line, never a FORESHORE-derived estimate. INCOIS does not "
        "publish a PFZ line for every area every day (annual fishing ban, cloud cover over "
        "the sensor, holidays) — a 'no line currently published' result is a valid, "
        "non-error answer, not a failure."
    ),
    schema=latlon_schema(),
    specialists=("GeospatialReasoning", "VisualizationAgent"),
    reads_sources=("incois_wfs",),
    cost="fast",
)
def find_nearest_pfz(lat: float, lon: float) -> ToolResult:
    """Nearest official INCOIS PFZ advisory line via ``IncoisWFS.nearest_pfz_line``.

    Returns ``ok=True, partial=True, missing=["incois_pfzlines"]`` when INCOIS has not
    issued a line for this area today — a designed, valid outcome — and ``ok=False``
    only on a genuine adapter/transport failure.
    """
    try:
        from ..sources.incois_wfs import IncoisWFS
    except Exception as exc:  # noqa: BLE001 — a missing adapter must not crash the tool
        return ToolResult(
            tool="find_nearest_pfz",
            ok=False,
            error=f"incois_wfs adapter unavailable: {type(exc).__name__}: {exc}",
            summary="Could not load the INCOIS PFZ adapter.",
            missing=["incois_wfs"],
        )

    try:
        result = IncoisWFS().nearest_pfz_line(lat, lon)
    except Exception as exc:  # noqa: BLE001 — network/parse failure, not "no line today"
        return ToolResult(
            tool="find_nearest_pfz",
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            summary=f"Failed to fetch the official INCOIS PFZ advisory line: {exc}",
            missing=["incois_pfzlines"],
        )

    if result is None:
        return ToolResult(
            tool="find_nearest_pfz",
            ok=True,
            partial=True,
            missing=["incois_pfzlines"],
            summary=(
                "No official INCOIS PFZ advisory line is currently published for this "
                "area. INCOIS does not issue a line for every sector every day (annual "
                "fishing ban, cloud cover, holidays) — this is a valid outcome, not an "
                "error, and no date-last-seen is available from this adapter."
            ),
            payload={"is_official": True},
        )

    obs, adapter_payload = result
    advisory_date_iso = adapter_payload.get("advisory_date")
    date_str = advisory_date_iso[:10] if isinstance(advisory_date_iso, str) else "an unrecorded date"
    distance_nm = obs.numeric if obs.numeric is not None else 0.0
    bearing = adapter_payload.get("bearing_deg")
    bearing_str = f"{bearing:.0f}" if isinstance(bearing, (int, float)) else "an unknown"

    summary = (
        f"Official INCOIS PFZ advisory line for {date_str} lies "
        f"{distance_nm:.1f} nm at {bearing_str} deg."
    )

    payload: dict[str, Any] = {
        "distance_nm": distance_nm,
        "bearing_deg": bearing,
        "advisory_date": advisory_date_iso,
        "closest_point": adapter_payload.get("closest_point"),
        "geometry": adapter_payload.get("geometry"),
        "is_official": True,
    }

    return ToolResult(
        tool="find_nearest_pfz",
        ok=True,
        observations=[obs],
        payload=payload,
        summary=summary,
    )


__all__ = ["find_nearest_pfz"]
