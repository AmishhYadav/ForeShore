"""Tool 7 — the official INCOIS Potential Fishing Zone (PFZ) advisory line.

This is deliberately narrow: it surfaces the government-issued PFZ line and nothing
else. Tool 8 (a FORESHORE-derived indicative PFZ product, for days INCOIS has not
published one) lives elsewhere and is intentionally not implemented here — mixing the
two in one module would make it too easy to accidentally hand back a derived product
under an "official" label, which is exactly the failure mode this file exists to avoid.
"""

from __future__ import annotations

from typing import Any

from ..models import ToolResult, utcnow
from .registry import latlon_schema, registry

#: How old the newest published PFZ line may be before this tool stops calling it an
#: answer to "where is the fishing zone today".
#:
#: INCOIS issues PFZ advisories on a roughly three-per-week cadence — weekdays, when the
#: sensor gets a cloud-free look — and suspends them entirely during the annual fishing
#: ban. Seven days is therefore a fortnight's worth of missed issues: comfortably past
#: any normal gap in the cadence, and short enough that a genuinely current line is never
#: flagged. It is a property of the *source's* publication schedule, not of the region,
#: which is why it lives here as a constant and not in a region file.
PFZ_ADVISORY_MAX_AGE_DAYS = 7.0


def _describe_age(age_days: float) -> str:
    """The advisory's age in the words a person would use. Never a bare float."""
    if age_days < 1.0:
        return "today"
    if age_days < 2.0:
        return "yesterday"
    if age_days < 14.0:
        return f"{int(round(age_days))} days ago"
    if age_days < 60.0:
        return f"about {int(round(age_days / 7.0))} weeks ago"
    if age_days < 730.0:
        return f"about {int(round(age_days / 30.44))} months ago"
    return f"about {age_days / 365.25:.0f} years ago"


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

    Three outcomes, all of them designed:

    * a line issued within :data:`PFZ_ADVISORY_MAX_AGE_DAYS` — ``ok=True``, reported as
      the current advisory;
    * a line older than that, or carrying no usable date — ``ok=True, partial=True,
      missing=["incois_pfzlines_current"]``. The line is still returned, but the summary
      leads with its age and says outright that it does not describe today. See the
      comment at that branch for why this check has to exist;
    * nothing published for this area — ``ok=True, partial=True,
      missing=["incois_pfzlines"]``.

    ``ok=False`` means a genuine adapter or transport failure and nothing else.
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

    # Invariant 4, at the one place it is easiest to breach by omission. The layer this
    # reads is currently frozen: probed live on 2026-09-06 it held 65 features nationally
    # and every one of them was Year=2021, Julian_day=248. Nothing upstream errors — the
    # WFS answers normally — so without an explicit age check the tool reports a
    # five-year-old line in the present tense and the answer reads as today's advisory.
    # It did exactly that before this check existed.
    age_days: float | None = None
    if obs.provenance.issued_at is not None:
        age_days = max(0.0, (utcnow() - obs.provenance.issued_at).total_seconds() / 86400.0)
    is_current = age_days is not None and age_days <= PFZ_ADVISORY_MAX_AGE_DAYS

    payload: dict[str, Any] = {
        "distance_nm": distance_nm,
        "bearing_deg": bearing,
        "advisory_date": advisory_date_iso,
        "advisory_age_days": round(age_days, 2) if age_days is not None else None,
        "is_current": is_current,
        "max_age_days": PFZ_ADVISORY_MAX_AGE_DAYS,
        "closest_point": adapter_payload.get("closest_point"),
        "geometry": adapter_payload.get("geometry"),
        "is_official": True,
    }

    if is_current:
        summary = (
            f"Official INCOIS Potential Fishing Zone advisory line, issued {date_str}, "
            f"lies {distance_nm:.1f} nm away at {bearing_str} degrees."
        )
        return ToolResult(
            tool="find_nearest_pfz",
            ok=True,
            observations=[obs],
            payload=payload,
            summary=summary,
        )

    # Stale, or undateable. Either way this is not an answer to "where is the zone
    # today", and it must not be offered as one. The line is still returned — it is real,
    # it is official, and where the fronts sat then is genuinely worth showing beside a
    # derived estimate — but the age leads the sentence so it cannot be read as current,
    # and `partial` plus `missing` tell the verdict engine that the current advisory is
    # an input the system does not have.
    if age_days is None:
        when_phrase = "on a date INCOIS did not record with it"
        age_clause = "how old it is cannot be determined"
    else:
        when_phrase = f"on {date_str}, {_describe_age(age_days)}"
        age_clause = "no newer line has been published for this coast since"
    summary = (
        f"No current official Potential Fishing Zone advisory is available for this "
        f"area. The most recent line INCOIS publishes here was issued {when_phrase}, and "
        f"{age_clause}. It lies {distance_nm:.1f} nm away at {bearing_str} degrees and is "
        f"shown for reference only — it does not describe where the fish are today."
    )
    return ToolResult(
        tool="find_nearest_pfz",
        ok=True,
        partial=True,
        missing=["incois_pfzlines_current"],
        observations=[obs],
        payload=payload,
        summary=summary,
    )


__all__ = ["find_nearest_pfz"]
