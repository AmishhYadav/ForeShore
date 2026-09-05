"""Tool 14 — nearest named landing centres, and the harbour handoff.

This tool exists so that ``DO_NOT_ADVISE`` hands off to a *named* place, never to
silence. Even when the landing-centre lookup itself fails, the tool still returns a
usable :class:`~foreshore.models.Handoff`-shaped payload pointing at the region's Coast
Guard contact — abstention always hands off to a person.
"""

from __future__ import annotations

from typing import Any

from ..config import ContactEntry, load_contact_directory, load_region
from ..models import Handoff, Observation, Provenance, ToolResult, utcnow
from .registry import latlon_schema, registry


def _coast_guard_payload(region: Any) -> dict[str, Any]:
    """The Coast Guard line, from the contact directory, region config as fallback.

    1554 is a real published national number, so it is the one handoff entry that is
    marked verified and may be offered to the UI as a dialable link.
    """
    directory = load_contact_directory()
    cg_region = region.coast_guard or {}
    entry = directory.coast_guard
    payload = entry.as_dict()
    # Region config still wins on naming/number if it carries them — a region swap must
    # be able to change the emergency number without editing the shared directory.
    if cg_region.get("name"):
        payload["authority_name"] = cg_region["name"]
    if cg_region.get("contact"):
        payload["contact"] = str(cg_region["contact"])
    payload["authority_type"] = "coast_guard"
    return payload


def _contact_fields(name: str | None, district: str | None) -> dict[str, Any]:
    """Directory lookup for one landing centre. Missing entry is not an error — the
    handoff is still named and the Coast Guard line is still shown alongside it."""
    entry: ContactEntry | None
    entry, kind = load_contact_directory().for_centre(name, district)
    if entry is None:
        return {
            "contact": None,
            "contact_label": None,
            "vhf_channel": None,
            "contact_verified": False,
            "authority_name": name,
        }
    if kind == "centre":
        # The centre has its own desk — its entry names the place AND who answers.
        label = entry.contact_label
        authority_name = entry.authority_name
    else:
        # District fallback: keep the coastal place name, attribute the number to the
        # office that actually answers it.
        label = (
            f"{entry.authority_name} — {entry.contact_label}"
            if entry.contact_label
            else entry.authority_name
        )
        authority_name = name
    return {
        "contact": entry.contact,
        "contact_label": label,
        "vhf_channel": entry.vhf_channel,
        "contact_verified": entry.verified,
        "authority_name": authority_name,
    }


@registry.tool(
    name="nearest_harbour",
    number=14,
    description=(
        "The nearest INCOIS-listed fishing landing centres to a position, for harbour "
        "return planning and as the named human-authority handoff whenever the system "
        "must abstain with DO_NOT_ADVISE. Abstention always hands off to a named place "
        "or the regional Coast Guard, never to silence."
    ),
    schema=latlon_schema(
        n={
            "type": "integer",
            "description": "Number of nearest landing centres to return.",
            "minimum": 1,
            "maximum": 20,
        }
    ),
    specialists=("GeospatialReasoning", "ReportingAgent"),
    reads_sources=("incois_wfs",),
    cost="fast",
)
def nearest_harbour(lat: float, lon: float, n: int = 3) -> ToolResult:
    """Nearest landing centres via ``IncoisWFS.nearest_landing_centres``, plus a named
    harbour handoff and the region's Coast Guard contact.

    Never fully fails: if the adapter is unavailable or resolves no centre, still
    returns ``ok=True, partial=True, missing=["landing_centres"]`` with the regional
    Coast Guard handoff in the payload, so a caller relying on this for a
    ``DO_NOT_ADVISE`` handoff always has *something* named to hand off to.
    """
    region = load_region()
    coast_guard = _coast_guard_payload(region)

    try:
        from ..sources.incois_wfs import IncoisWFS
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            tool="nearest_harbour",
            ok=True,
            partial=True,
            missing=["landing_centres"],
            summary=(
                "INCOIS landing-centre adapter unavailable; handing off to the regional "
                "Coast Guard instead."
            ),
            payload={"centres": [], "handoff": None, "coast_guard": coast_guard},
            error=f"incois_wfs adapter unavailable: {type(exc).__name__}: {exc}",
        )

    wfs = IncoisWFS(region=region)

    try:
        centres = wfs.nearest_landing_centres(lat, lon, n)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            tool="nearest_harbour",
            ok=True,
            partial=True,
            missing=["landing_centres"],
            summary=(
                f"Could not resolve a named landing centre ({type(exc).__name__}: {exc}); "
                "handing off to the regional Coast Guard instead."
            ),
            payload={"centres": [], "handoff": None, "coast_guard": coast_guard},
        )

    if not centres:
        return ToolResult(
            tool="nearest_harbour",
            ok=True,
            partial=True,
            missing=["landing_centres"],
            summary=(
                "No named landing centre could be resolved for this position; handing "
                "off to the regional Coast Guard instead."
            ),
            payload={"centres": [], "handoff": None, "coast_guard": coast_guard},
        )

    # Provenance: reuse the region-bbox landing-centres fetch's metadata. This is the
    # same INCOIS dataset `nearest_landing_centres` selected from (it only widens to a
    # national bbox on the rare edge case of nothing inside the region), so it is an
    # honest source record even when that widening happened.
    try:
        _feats, raw = wfs.landing_centres(bbox=region.bbox)
        prov = wfs.provenance(
            raw,
            valid_from=raw.acquired_at,
            is_derived=False,
            notes="INCOIS PFZ_LandingCentres:LandingCenters_29Apr2024.",
        )
    except Exception:  # noqa: BLE001 — never let a provenance re-fetch sink a good answer
        prov = Provenance(
            source_id="incois_wfs",
            source_name="INCOIS GeoServer (WFS)",
            authority="INCOIS",
            url="https://incois.gov.in/geoserver/PFZ_LandingCentres/ows",
            acquired_at=utcnow(),
            issued_at=utcnow(),
            notes="provenance reconstructed; original fetch metadata unavailable",
        )

    observations = [
        Observation(
            variable="landing_centre_distance",
            value=round(c.distance_nm, 3) if c.distance_nm is not None else 0.0,
            unit="nm",
            lat=c.lat,
            lon=c.lon,
            valid_time=prov.acquired_at,
            provenance=prov,
            qualifiers={"name": c.name, "district": c.district, "state": c.state, "lat": c.lat, "lon": c.lon},
        )
        for c in centres
    ]

    nearest = centres[0]
    fields = _contact_fields(nearest.name, nearest.district)
    authority_name = fields["authority_name"] or nearest.name

    alternates: list[dict[str, Any]] = []
    for c in centres[1:3]:
        alt = _contact_fields(c.name, c.district)
        alternates.append(
            {
                "authority_name": alt["authority_name"] or c.name,
                "authority_type": "landing_centre",
                "district": c.district,
                "contact": alt["contact"],
                "contact_label": alt["contact_label"],
                "contact_verified": alt["contact_verified"],
                "vhf_channel": alt["vhf_channel"],
                "distance_nm": c.distance_nm,
            }
        )

    handoff = Handoff(
        reason="Nearest named landing centre for harbour return / DO_NOT_ADVISE handoff.",
        authority_name=authority_name,
        authority_type="landing_centre",
        contact=fields["contact"],
        contact_label=fields["contact_label"],
        contact_verified=fields["contact_verified"],
        vhf_channel=fields["vhf_channel"],
        district=nearest.district,
        lat=nearest.lat,
        lon=nearest.lon,
        distance_nm=nearest.distance_nm,
        alternates=tuple(alternates),
        provenance=prov,
    )

    district_bit = f", {nearest.district} district" if nearest.district else ""
    dist_bit = f"{nearest.distance_nm:.1f} nm" if nearest.distance_nm is not None else "an unknown distance"
    summary = f"Nearest landing centre: {nearest.name}{district_bit}, {dist_bit} away."

    payload = {
        "centres": [c.to_dict() for c in centres],
        "handoff": handoff.to_dict(),
        "coast_guard": coast_guard,
    }
    return ToolResult(
        tool="nearest_harbour", ok=True, observations=observations, payload=payload, summary=summary
    )


__all__ = ["nearest_harbour"]
