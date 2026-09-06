"""Tool 18 -- FORESHORE's chlorophyll + favourable-SST productive-waters ranking.

Answers the problem-statement bullet verbatim: *"Which regions show high chlorophyll
concentration and favourable sea surface temperature?"* Nothing in the registry answered
this before this tool existed -- the planner had no tool that combined the two signals
the question actually names, so a query shaped like it fell through to a generic safety
check and came back as a geofence dump.

**Relationship to tool 8 (`pfz_derived.py`, `derive_pfz_zones`).** That tool leads with
an SST *frontal gradient* and treats chlorophyll as an opportunistic cross-check reached
through a three-step fallback chain -- because its job is "where is the front", and a
front is fundamentally a gradient signal. This tool inverts the emphasis to match the PS
bullet's own wording: chlorophyll is the lead signal ("high chlorophyll concentration"),
sea-surface temperature is the qualifying band ("favourable ... temperature"), not a
gradient. Both tools end up reading the same NOAA CoastWatch adapter
(`sources/oceancolour.py`) for chlorophyll, because that adapter is *why* chlorophyll
data reaches this system at all -- the INCOIS OSF `chl` grid is a Pacific-Islands-basin
product that has never once covered this coast (docs/DECISIONS.md D1). Two different
questions, two different combinations of the same underlying signals, both honestly
reported when a signal is missing rather than silently answered on half of it.

**This is never the INCOIS PFZ advisory.** The official advisory line is reported
separately by tool 7. Every polygon this tool emits carries ``is_derived: true`` in its
GeoJSON properties, every number rides an ``Observation`` whose ``Provenance.is_derived``
is ``True``, and the summary opens by saying so -- a caller reading only the first
sentence out of context must still not be able to mistake this for the official product.

**Method.** Chlorophyll (NOAA CoastWatch gap-filled VIIRS composite) is regridded onto
the INCOIS OSF sea-surface-temperature grid -- the coarser grid this system already
treats as authoritative and routes/thresholds against everywhere else. A zone is a
connected component of cells simultaneously in the top slice of the chlorophyll field
present in the requested area (:data:`CHLOROPHYLL_PERCENTILE`, a *relative* cutoff for
the same reason ``pfz_derived.GRADIENT_PERCENTILE`` is relative -- an absolute mg/m^3
constant tuned for one bloom season would misfire the moment a region-config swap
changes the basin, CLAUDE.md invariant 6) and inside a favourable SST band
(:data:`SST_FAVOURABLE_BAND_DEGC`, or a region-config override -- see
:func:`_resolve_sst_band`). Whether an SST front coincides with each zone is reported
too, using the *same* relative gradient test tool 8 uses for its own front detection
(``pfz_derived.GRADIENT_PERCENTILE`` and ``_gradient_magnitude_per_km``, imported rather
than re-derived -- a chlorophyll patch riding a real thermal front is a stronger signal
than a chlorophyll patch alone, but is never required for a zone to qualify).

**Geometry and thresholding are plain numpy** (``Grid.mask_to_polygons``, connected-
component labelling from ``store/grids.py``) -- no LLM involvement in the arithmetic or
the contouring, per CLAUDE.md's tools convention.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np

from ..config import load_region
from ..models import (
    EARTH_RADIUS_M,
    NM_PER_M,
    Observation,
    Provenance,
    ToolResult,
    bearing_deg,
    haversine_nm,
)
from ..store.grids import Grid, _cell_edges, _label_components, regrid_to
from .pfz_derived import GRADIENT_PERCENTILE, MIN_CELLS_PER_ZONE, _gradient_magnitude_per_km
from .registry import registry

#: Percentile of the chlorophyll field (over finite cells in the requested bbox) used as
#: the "high chlorophyll" cutoff -- the PS bullet's own word. A *relative* cutoff, for
#: the identical reason ``pfz_derived.GRADIENT_PERCENTILE`` is relative: an absolute
#: mg/m^3 constant tuned for this coast in one bloom season would misfire the moment
#: CLAUDE.md invariant 6 (region config only) is exercised against a different basin or
#: the same basin in a different season. "Top fifth of the chlorophyll actually present
#: here, right now" is a stable definition of "high" even though the absolute mg/m^3
#: value that means varies by season and coast.
CHLOROPHYLL_PERCENTILE = 80.0

#: Fallback favourable SST band in degC, used whenever the active region config carries
#: no ``sources.sst_favourable_band_degc`` override (see :func:`_resolve_sst_band`). Not
#: a single external paper's number -- INCOIS does not publish its internal PFZ
#: thresholds, and the published attempts to reproduce them disagree by coast and
#: season. Anchored instead to two facts this system has direct documented evidence for
#: on *this* water body:
#:   - lower bound ~26 degC: the Palk Bay / Gulf of Mannar shelf's own seasonal minimum,
#:     reached in the NE-monsoon cool season;
#:   - upper bound capped at 30.5 degC, half a degree under the documented Gulf of
#:     Mannar coral-bleaching stress threshold (observed bleaching at SST > 31 degC for
#:     this exact water body) -- water that hot is a thermal-stress signal, not a
#:     fish-aggregating front.
#: This is a working default for one coast, not a universal constant -- the region-config
#: override exists for exactly that reason.
SST_FAVOURABLE_BAND_DEGC: tuple[float, float] = (26.0, 30.5)

#: 16-point compass words. The prose reports one of these, never a raw bearing number --
#: a fisherman reads "south-southeast"; the map (payload ``bearing_deg``, and every
#: per-zone Observation's ``qualifiers["bearing_deg"]``) carries the exact figure.
_COMPASS_POINTS = (
    "north", "north-northeast", "northeast", "east-northeast",
    "east", "east-southeast", "southeast", "south-southeast",
    "south", "south-southwest", "southwest", "west-southwest",
    "west", "west-northwest", "northwest", "north-northwest",
)


def _compass_point(bearing: float) -> str:
    idx = int(((bearing % 360.0) + 11.25) // 22.5) % 16
    return _COMPASS_POINTS[idx]


def _resolve_sst_band(region: Any) -> tuple[float, float]:
    """``sources.sst_favourable_band_degc: [min, max]`` in the active region config wins
    when present; otherwise :data:`SST_FAVOURABLE_BAND_DEGC`. Never an unexplained
    literal inline -- either it traces to config or it traces to the module comment
    above naming exactly why those two numbers were chosen."""
    override = region.source("sst_favourable_band_degc")
    if override is not None:
        try:
            lo, hi = float(override[0]), float(override[1])
            if lo < hi:
                return lo, hi
        except (TypeError, ValueError, IndexError):
            pass
    return SST_FAVOURABLE_BAND_DEGC


def _parse_when(when: str | None) -> tuple[datetime | None, str | None]:
    """Tolerant ISO-8601 parse -- same contract as ``pfz_derived._parse_when``. Kept as
    a small local copy rather than a cross-module import: it is a few lines, and this
    tool's actual shared surface with tool 8 (the geometry/threshold helpers) is what
    matters for staying in lockstep, not this."""
    if when is None:
        return None, None
    s = when.strip()
    if not s:
        return None, None
    try:
        s2 = f"{s[:-1]}+00:00" if s.endswith("Z") else s
        return datetime.fromisoformat(s2), None
    except ValueError:
        return None, "could not parse the requested date; used the nearest available data instead"


def _empty_feature_collection() -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": []}


def _missing_result(missing_key: str, reason: str) -> ToolResult:
    """Shared shape for both abstention paths (rule 5): chlorophyll missing or SST
    missing degrade identically -- ``ok=True, partial=True`` and a summary that names
    what was missing in ordinary words, never a silent fall-back to the other signal
    alone. A fisherman must not be told where the fish are on half the evidence without
    being told it is half."""
    subject = "chlorophyll concentration" if missing_key == "chlorophyll" else "sea-surface temperature"
    summary = (
        "FORESHORE-derived, INDICATIVE productive-waters estimate could not be made: it "
        "needs chlorophyll concentration together with sea-surface temperature, and "
        f"{subject} was not available for this area and date. Abstaining rather than "
        "answering on half the evidence."
    )
    return ToolResult(
        tool="find_productive_waters",
        ok=True,
        partial=True,
        missing=[missing_key],
        observations=[],
        payload={
            "zones": _empty_feature_collection(),
            "reference_point": None,
            "method": {"description": "not computed", "reason": reason},
            "chlorophyll_source": None,
            "sst_source": None,
            "sst_band_degc": None,
            "chlorophyll_percentile": CHLOROPHYLL_PERCENTILE,
        },
        summary=summary,
    )


def _productive_zone_records(
    mask: np.ndarray,
    lats: np.ndarray,
    lons: np.ndarray,
    sst: np.ndarray,
    chl: np.ndarray,
    grad_km: np.ndarray,
    grad_threshold: float | None,
    ref_lat: float,
    ref_lon: float,
) -> list[dict[str, Any]]:
    """Per-connected-component statistics, in the same label order
    ``Grid.mask_to_polygons`` returns polygons for the identical ``mask`` -- see
    ``pfz_derived._zone_records``'s docstring for why that ordering guarantee holds
    (both are pure, deterministic functions of ``mask`` alone).
    """
    if not mask.any():
        return []
    labels = _label_components(mask)
    lat_edges = _cell_edges(lats)
    lon_edges = _cell_edges(lons)
    flat_labels = labels[labels >= 0]
    unique_labels, counts = np.unique(flat_labels, return_counts=True)

    records: list[dict[str, Any]] = []
    for lbl, count in zip(unique_labels.tolist(), counts.tolist()):
        if count < MIN_CELLS_PER_ZONE:
            continue
        rows, cols = np.nonzero(labels == lbl)
        lat0 = lat_edges[rows]
        lat1 = lat_edges[rows + 1]
        lon0 = lon_edges[cols]
        lon1 = lon_edges[cols + 1]
        lat_span_rad = np.radians(np.abs(lat1 - lat0))
        lon_span_rad = np.radians(np.abs(lon1 - lon0))
        cell_lat_rad = np.radians(lats[rows])
        cell_area_m2 = (lat_span_rad * EARTH_RADIUS_M) * (lon_span_rad * EARTH_RADIUS_M * np.cos(cell_lat_rad))
        area_nm2 = float(np.sum(cell_area_m2)) * (NM_PER_M ** 2)

        mean_chl = float(np.nanmean(chl[rows, cols]))
        mean_sst = float(np.nanmean(sst[rows, cols]))
        centroid_lat = float(np.mean(lats[rows]))
        centroid_lon = float(np.mean(lons[cols]))

        front_coincides: bool | None = None
        if grad_threshold is not None:
            cell_grad = grad_km[rows, cols]
            front_coincides = bool(
                np.isfinite(cell_grad).any() and np.nanmax(cell_grad) >= grad_threshold
            )

        records.append({
            "cell_count": int(count),
            "area_nm2": area_nm2,
            "mean_chlorophyll_mg_m3": mean_chl,
            "mean_sst_degc": mean_sst,
            "centroid_lat": centroid_lat,
            "centroid_lon": centroid_lon,
            "front_coincides": front_coincides,
            "distance_nm": haversine_nm(ref_lat, ref_lon, centroid_lat, centroid_lon),
            "bearing_deg": bearing_deg(ref_lat, ref_lon, centroid_lat, centroid_lon),
        })
    return records


@registry.tool(
    name="find_productive_waters",
    number=18,
    description=(
        "Rank the productive fishing waters in an area by combining chlorophyll "
        "concentration with sea surface temperature and the strength of the thermal "
        "front between them. Returns named zones ordered by distance from a reference "
        "point, each with its mean chlorophyll, mean SST, area and bearing. This is a "
        "FORESHORE-derived indicative product computed from published satellite fields "
        "-- it is never the official INCOIS Potential Fishing Zone advisory, which "
        "find_nearest_pfz reports separately."
    ),
    schema={
        "type": "object",
        "properties": {
            "bbox": {
                "type": ["array", "null"],
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
                "description": (
                    "Optional [minlon, minlat, maxlon, maxlat] EPSG:4326 override. "
                    "Defaults to the active region's bbox."
                ),
            },
            "when": {
                "type": ["string", "null"],
                "description": (
                    "Optional ISO-8601 target date/time. Omit or null for the latest "
                    "available chlorophyll/SST fields."
                ),
            },
            "lat": {
                "type": ["number", "null"],
                "description": (
                    "Optional reference latitude the returned zones are ranked from. "
                    "Defaults to the active region's first anchor port."
                ),
            },
            "lon": {
                "type": ["number", "null"],
                "description": (
                    "Optional reference longitude the returned zones are ranked from. "
                    "Defaults to the active region's first anchor port."
                ),
            },
            "limit": {
                "type": "integer",
                "default": 5,
                "description": "Maximum number of ranked zones to return.",
            },
        },
        "required": [],
    },
    specialists=("OceanAnalytics", "MarineDataDiscovery", "VisualizationAgent"),
    reads_sources=("noaa_coastwatch", "incois_osf_sst"),
    emits_derived=True,
    cost="slow",
)
def find_productive_waters(
    bbox: list[float] | None = None,
    when: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    limit: int = 5,
) -> ToolResult:
    """Rank FORESHORE's own chlorophyll+SST productive-waters zones by distance from a
    reference point. Never raises: a missing chlorophyll or SST input degrades to
    ``ok=True, partial=True`` with a named ``missing`` entry, never a crash and never an
    answer computed from only one of the two signals under the same confident wording.
    """
    region = load_region()
    bbox_use = tuple(float(v) for v in bbox) if bbox else region.bbox
    when_dt, when_note = _parse_when(when)
    limit_n = int(limit) if limit else 5
    if limit_n < 1:
        limit_n = 1

    if lat is not None and lon is not None:
        ref_lat, ref_lon = float(lat), float(lon)
        ref_label = "the requested position"
    elif region.anchor_ports:
        port = region.anchor_ports[0]
        ref_lat, ref_lon = port.lat, port.lon
        ref_label = port.name
    else:
        ref_lat, ref_lon = region.centre
        ref_label = "the region centre"

    # -- step 1: chlorophyll -----------------------------------------------------------
    try:
        from ..sources.oceancolour import OceanColour

        chl_gs = OceanColour(region=region).chlorophyll_slice(bbox_use, at=when_dt, product="gapfilled")
        chl_arr = chl_gs.variables.get("chlorophyll_a")
    except Exception as exc:  # noqa: BLE001 -- chlorophyll unavailable is an abstention, not a crash
        return _missing_result("chlorophyll", f"{type(exc).__name__}: {exc}")

    if chl_arr is None or chl_arr.size == 0 or not np.isfinite(chl_arr).any():
        return _missing_result(
            "chlorophyll", "the chlorophyll field carried no valid readings for this area and date",
        )

    # -- step 2: sea-surface temperature -------------------------------------------------
    try:
        from ..sources.incois_thredds import IncoisThredds

        sst_gs = IncoisThredds(region=region).slice("sst", at=when_dt, bbox=bbox_use)
        sst_arr = sst_gs.variables.get("sea_surface_temperature")
    except Exception as exc:  # noqa: BLE001 -- SST unavailable is an abstention, not a crash
        return _missing_result("sea_surface_temperature", f"{type(exc).__name__}: {exc}")

    if sst_arr is None or sst_arr.size == 0 or not np.isfinite(sst_arr).any():
        return _missing_result(
            "sea_surface_temperature",
            "the sea-surface-temperature field carried no valid readings for this area and date",
        )

    # -- step 3: regrid chlorophyll onto the SST grid ------------------------------------
    chl_grid = Grid(
        name="chlorophyll_a", values=chl_arr, lats=chl_gs.lats, lons=chl_gs.lons,
        times=None, unit="mg/m^3", attrs={},
    )
    chl_on_sst = regrid_to(chl_grid, sst_gs.lats, sst_gs.lons)

    if not np.isfinite(chl_on_sst).any():
        return _missing_result(
            "chlorophyll",
            "the chlorophyll field did not overlap the sea-surface-temperature grid for this area and date",
        )

    # -- step 4: zone mask -- chlorophyll percentile AND favourable SST band ------------
    finite_chl = chl_on_sst[np.isfinite(chl_on_sst)]
    chl_threshold = float(np.percentile(finite_chl, CHLOROPHYLL_PERCENTILE))
    sst_lo, sst_hi = _resolve_sst_band(region)

    mask = (
        np.isfinite(chl_on_sst) & (chl_on_sst >= chl_threshold)
        & np.isfinite(sst_arr) & (sst_arr >= sst_lo) & (sst_arr <= sst_hi)
    )

    # -- SST-front coincidence -- the same relative gradient test tool 8 uses -----------
    grad_km = _gradient_magnitude_per_km(sst_arr, sst_gs.lats, sst_gs.lons)
    finite_grad = grad_km[np.isfinite(grad_km)]
    grad_threshold = float(np.percentile(finite_grad, GRADIENT_PERCENTILE)) if finite_grad.size else None

    # -- step 5: polygonise ---------------------------------------------------------------
    sst_grid = Grid(
        name="sea_surface_temperature", values=sst_arr, lats=sst_gs.lats, lons=sst_gs.lons,
        times=None, unit="degC", attrs={},
    )
    polygons: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    if mask.any():
        polygons = sst_grid.mask_to_polygons(mask, min_cells=MIN_CELLS_PER_ZONE)
        records = _productive_zone_records(
            mask, sst_gs.lats, sst_gs.lons, sst_arr, chl_on_sst, grad_km, grad_threshold, ref_lat, ref_lon,
        )

    method = (
        "Chlorophyll (NOAA CoastWatch gap-filled composite) regridded onto the INCOIS "
        "OSF sea-surface-temperature grid; a zone is a connected component of cells in "
        f"the top {100.0 - CHLOROPHYLL_PERCENTILE:.0f}% of chlorophyll present in the "
        f"requested area and inside the {sst_lo:.1f}-{sst_hi:.1f} degC favourable SST "
        f"band, polygonised via connected-component labelling and dropping components "
        f"under {MIN_CELLS_PER_ZONE} cells."
    )

    if not records:
        # A valid, non-error outcome (rule: "no zone clears the thresholds"), not a
        # failure to abstain over -- nothing here needed missing evidence to answer.
        summary = (
            "FORESHORE-derived, INDICATIVE productive-waters estimate -- this combines "
            "chlorophyll concentration with sea-surface temperature, never the official "
            "INCOIS Potential Fishing Zone advisory, which is reported separately. No "
            "waters in this area currently stand out: nothing cleared both the "
            "chlorophyll and favourable-temperature thresholds together, which is a "
            "valid reading of the data, not a failed one."
        )
        return ToolResult(
            tool="find_productive_waters",
            ok=True,
            observations=[],
            payload={
                "zones": _empty_feature_collection(),
                "reference_point": [ref_lat, ref_lon],
                "method": method,
                "chlorophyll_source": chl_gs.provenance.source_name,
                "sst_source": sst_gs.provenance.source_name,
                "sst_band_degc": [sst_lo, sst_hi],
                "chlorophyll_percentile": CHLOROPHYLL_PERCENTILE,
                "when_note": when_note,
            },
            summary=summary,
        )

    # -- step 7: rank by distance from the reference point, ascending; cap at limit -----
    zipped = list(zip(polygons, records))
    zipped.sort(key=lambda pr: pr[1]["distance_nm"])
    zipped = zipped[:limit_n]

    # A per-variable Provenance each, not one shared record for all three: a chlorophyll
    # number must trace to the chlorophyll grid that actually supplied it (its own url,
    # acquisition time and native resolution), not silently borrow the SST granule's --
    # the same reasoning ``pfz_derived.py``'s own ``chl_prov`` vs ``prov`` split
    # documents. The zone geometry itself (distance/bearing) traces to neither source
    # alone, so it gets a third, combined record naming both.
    sst_prov = Provenance(
        source_id=sst_gs.provenance.source_id,
        source_name=sst_gs.provenance.source_name,
        authority=sst_gs.provenance.authority,
        url=sst_gs.provenance.url,
        acquired_at=sst_gs.provenance.acquired_at,
        issued_at=sst_gs.provenance.issued_at,
        valid_from=sst_gs.provenance.valid_from,
        valid_to=sst_gs.provenance.valid_to,
        spatial_resolution_m=sst_gs.provenance.spatial_resolution_m,
        is_derived=True,
        notes=(
            "zone spatial mean over the INCOIS OSF sea-surface-temperature grid, file "
            f"date {sst_gs.file_date.isoformat()}"
        ),
    )
    chl_prov = Provenance(
        source_id=chl_gs.provenance.source_id,
        source_name=chl_gs.provenance.source_name,
        authority=chl_gs.provenance.authority,
        url=chl_gs.provenance.url,
        acquired_at=chl_gs.provenance.acquired_at,
        issued_at=chl_gs.provenance.issued_at,
        valid_from=chl_gs.provenance.valid_from,
        valid_to=chl_gs.provenance.valid_to,
        spatial_resolution_m=chl_gs.provenance.spatial_resolution_m,
        is_derived=True,
        notes=(
            "zone spatial mean, nearest-neighbour regridded onto the INCOIS OSF SST "
            f"grid; native chlorophyll file date {chl_gs.file_date.isoformat()}; "
            "chlorophyll composites typically run 2-3 days behind live conditions"
        ),
    )
    zone_prov = Provenance(
        source_id="foreshore_productive_waters",
        source_name="FORESHORE derived productive-waters zone (chlorophyll + SST band)",
        authority="derived",
        url=sst_gs.provenance.url,
        acquired_at=sst_gs.provenance.acquired_at,
        issued_at=sst_gs.provenance.issued_at,
        valid_from=sst_gs.provenance.valid_from,
        valid_to=sst_gs.provenance.valid_to,
        spatial_resolution_m=sst_gs.provenance.spatial_resolution_m,
        is_derived=True,
        notes=(
            "great-circle distance and bearing from the reference point to the zone "
            f"centroid; zone defined from chlorophyll (file date "
            f"{chl_gs.file_date.isoformat()}) at or above the "
            f"{CHLOROPHYLL_PERCENTILE:.0f}th percentile combined with sea-surface "
            f"temperature (file date {sst_gs.file_date.isoformat()}) inside "
            f"{sst_lo:.1f}-{sst_hi:.1f} degC"
        ),
    )

    observations: list[Observation] = []
    features: list[dict[str, Any]] = []
    zone_sentences: list[str] = []
    _ORDINALS = ("the closest", "the next", "the third")

    for i, (geom, rec) in enumerate(zipped):
        rank = i + 1
        zone_id = f"productive_{rank}"
        qualifiers = {
            "zone_id": zone_id,
            "zone_rank": rank,
            "centroid_lat": rec["centroid_lat"],
            "centroid_lon": rec["centroid_lon"],
            "bearing_deg": rec["bearing_deg"],
            "area_nm2": rec["area_nm2"],
        }

        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "zone_id": zone_id,
                "zone_rank": rank,
                "is_derived": True,
                "centroid_lat": rec["centroid_lat"],
                "centroid_lon": rec["centroid_lon"],
                "bearing_deg": rec["bearing_deg"],
                "distance_nm": rec["distance_nm"],
                "area_nm2": rec["area_nm2"],
                "mean_chlorophyll_mg_m3": rec["mean_chlorophyll_mg_m3"],
                "mean_sst_degc": rec["mean_sst_degc"],
                "sst_front_coincides": rec["front_coincides"],
            },
        })

        observations.append(Observation(
            variable="zone_mean_chlorophyll", value=rec["mean_chlorophyll_mg_m3"], unit="mg/m^3",
            lat=rec["centroid_lat"], lon=rec["centroid_lon"], valid_time=chl_gs.valid_time,
            provenance=chl_prov, qualifiers=dict(qualifiers),
        ))
        observations.append(Observation(
            variable="zone_mean_sea_surface_temperature", value=rec["mean_sst_degc"], unit="degC",
            lat=rec["centroid_lat"], lon=rec["centroid_lon"], valid_time=sst_gs.valid_time,
            provenance=sst_prov, qualifiers=dict(qualifiers),
        ))
        observations.append(Observation(
            variable="zone_distance", value=rec["distance_nm"], unit="nm",
            lat=rec["centroid_lat"], lon=rec["centroid_lon"], valid_time=sst_gs.valid_time,
            provenance=zone_prov, qualifiers=dict(qualifiers),
        ))

        if rank <= 3:
            compass = _compass_point(rec["bearing_deg"])
            zone_sentences.append(
                f"{_ORDINALS[rank - 1]} is about {rec['distance_nm']:.0f} nm {compass} of "
                f"{ref_label}, averaging {rec['mean_chlorophyll_mg_m3']:.1f} mg/m^3 "
                f"chlorophyll over water at {rec['mean_sst_degc']:.1f} degC"
            )

    summary = (
        "FORESHORE-derived, INDICATIVE productive-waters estimate -- this combines "
        "chlorophyll concentration with sea-surface temperature, never the official "
        "INCOIS Potential Fishing Zone advisory, which is reported separately. Ranked "
        f"by distance from {ref_label}: " + "; ".join(zone_sentences) + ". Chlorophyll "
        "composites typically run a few days behind live conditions."
    )

    return ToolResult(
        tool="find_productive_waters",
        ok=True,
        observations=observations,
        payload={
            "zones": {"type": "FeatureCollection", "features": features},
            "reference_point": [ref_lat, ref_lon],
            "method": method,
            "chlorophyll_source": chl_gs.provenance.source_name,
            "sst_source": sst_gs.provenance.source_name,
            "sst_band_degc": [sst_lo, sst_hi],
            "chlorophyll_percentile": CHLOROPHYLL_PERCENTILE,
            "when_note": when_note,
        },
        summary=summary,
    )


__all__ = ["find_productive_waters"]
