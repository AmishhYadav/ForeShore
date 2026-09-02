"""Tool 8 -- FORESHORE's own INDICATIVE derivation of likely fishing zones.

This is deliberately the mirror image of ``pfz.py`` (tool 7): that module hands back
the government-issued INCOIS PFZ advisory line and refuses to compute anything of its
own; this module computes an estimate and refuses to call it official. Every polygon
this tool emits carries ``is_derived: true`` in its GeoJSON properties, every number it
emits rides an ``Observation`` whose ``Provenance.is_derived`` is ``True``, and the
summary opens by naming the product as derived and indicative before it says anything
else -- so a caller quoting the first sentence out of context still cannot mistake this
for the INCOIS advisory.

**Method, and why (see docs/DECISIONS.md D1 for the full finding).** The plan originally
called for a chlorophyll/SST two-signal derivation, mirroring INCOIS's own operational
PFZ method. Live probing (2026-08-31) found that the INCOIS OSF ``chl`` feed
(``VIIRS-SNPP-Roll-*-4KM-PICountries-CHL.nc``) publishes a Pacific-Islands-basin grid
(roughly 130-215 degrees E) that does not reach this system's 65-95 degrees E
Indian-Ocean bbox at all -- a real, current, 4 km product, for the wrong ocean. This
tool therefore derives from **INCOIS OSF SST frontal gradients** alone (one of the two
signals INCOIS's own PFZ derivation actually uses), and *attempts* the chlorophyll field
fresh on every call: if a future region-config swap (CLAUDE.md invariant 6) points this
system at a basin the ``chl`` grid does cover, the second signal engages automatically
via the same code path, with no special-casing for "this region doesn't have it". Where
it does not cover the requested bbox/date, that is reported explicitly in
``chlorophyll_available`` and the reason, never silently dropped and never patched over
with a different basin's numbers.

**Geometry and thresholding are plain numpy** (:func:`_gradient_magnitude_per_km`,
``Grid.threshold_mask``, ``Grid.mask_to_polygons`` from ``store/grids.py``) -- no LLM
involvement in the arithmetic or the contouring, per CLAUDE.md's tools convention.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import numpy as np

from ..config import load_region
from ..models import EARTH_RADIUS_M, Observation, Provenance, ToolResult
from ..store.grids import Grid, _cell_edges, _label_components, regrid_to
from .registry import registry

#: Percentile of the gradient-magnitude field (computed over the requested bbox alone)
#: used as the front-detection cutoff. A *relative* cutoff is used instead of an
#: absolute degC/km number pulled from a single paper, because a genuine front's
#: gradient magnitude varies with grid resolution, mixed-layer depth and season -- an
#: absolute constant tuned for this coast in August would silently mis-fire the moment
#: CLAUDE.md invariant 6 (region config only) is exercised and this tool runs against a
#: different config-swapped region, or the same region in a different month. "The
#: strongest slice of the gradient field, right here, right now" is a stable definition
#: of a front regardless of any of that. 85 is chosen deliberately over the extremes:
#: 50 would flag half the domain (fronts are a minority feature of the SST field, not
#: the norm), while 99 would return at most a handful of pixels on a gently-sloped day
#: with no sharp front at all, undercounting genuine candidates. 85 keeps "top ~15% of
#: the gradient field" as the working definition of a front -- a minority, not a
#: vanishing fraction, of the sea surface on a given day.
GRADIENT_PERCENTILE = 85.0

#: Minimum 4-connected cell count for a component to survive as a zone -- matches
#: ``Grid.mask_to_polygons``'s own default. Small enough to keep a genuine, compact
#: frontal filament; large enough to drop single-pixel sensor noise spikes.
MIN_CELLS_PER_ZONE = 4

#: Documented live (2026-08-31, docs/DECISIONS.md D1; also recorded in
#: sources/incois_thredds.py's module docstring) rather than invented here: the OSF
#: chlorophyll grid is a VIIRS-SNPP 4 km "PICountries" (Pacific Islands Countries)
#: rolling composite. Quoted verbatim when a chlorophyll fetch fails in the
#: out-of-coverage way D1 describes, so the abstention reason is traceable to a probed
#: fact rather than a guess.
_CHL_KNOWN_COVERAGE_NOTE = (
    "the INCOIS OSF chlorophyll grid (VIIRS-SNPP 4 km 'PICountries' rolling composite) "
    "is documented to cover roughly 130-215 degrees E, the Pacific Islands basin -- see "
    "docs/DECISIONS.md D1"
)


def _parse_when(when: str | None) -> tuple[datetime | None, str | None]:
    """Tolerant ISO-8601 parse. ``None``/unparsable both fall back to "nearest
    available catalogue date" inside :meth:`IncoisThredds.slice` -- never "today"."""
    if when is None:
        return None, None
    s = when.strip()
    if not s:
        return None, None
    try:
        s2 = f"{s[:-1]}+00:00" if s.endswith("Z") else s
        return datetime.fromisoformat(s2), None
    except ValueError:
        return None, f"could not parse when={when!r}; used the nearest available catalogue date instead"


def _empty_feature_collection() -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": []}


def _gradient_magnitude_per_km(field: np.ndarray, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Magnitude of the horizontal gradient of ``field`` in units-per-km.

    ``numpy.gradient`` is asked for the derivative with respect to the coordinate
    arrays themselves (``lats``, ``lons``, decimal degrees), so its raw output is
    units-per-*degree* -- and a degree is not a metric unit: a degree of latitude is
    ~111 km everywhere, but a degree of longitude shrinks towards the poles by
    ``cos(latitude)``. Both axes are converted to true great-circle metres here using
    :data:`EARTH_RADIUS_M`, the same constant every other geodesy computation in this
    codebase uses (``models.haversine_m`` et al.) -- not a separately-typed "111320"
    literal that could silently drift out of sync with it.
    """
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    meters_per_deg_lat = EARTH_RADIUS_M * math.pi / 180.0
    meters_per_deg_lon_per_row = meters_per_deg_lat * np.cos(np.radians(lats))  # shape (n_lat,)
    with np.errstate(invalid="ignore"):
        d_dlat, d_dlon = np.gradient(field, lats, lons)
    d_dy = d_dlat / meters_per_deg_lat
    d_dx = d_dlon / meters_per_deg_lon_per_row[:, None]
    magnitude_per_m = np.sqrt(d_dx**2 + d_dy**2)
    return magnitude_per_m * 1000.0  # -> per km


def _zone_records(
    mask: np.ndarray,
    lats: np.ndarray,
    lons: np.ndarray,
    sst: np.ndarray,
    grad_km: np.ndarray,
    chl_on_sst: np.ndarray | None,
    chl_grad_on_sst: np.ndarray | None,
    chl_threshold: float | None,
) -> list[dict[str, Any]]:
    """Per-connected-component summary statistics.

    Iterates the *same* ``_label_components`` -> ``np.unique`` -> filter-by-count
    sequence that ``Grid.mask_to_polygons`` runs internally, on the identical ``mask``
    array -- both are pure deterministic functions of ``mask``, so this list lines up
    1:1, in order, with the polygon list ``mask_to_polygons`` returns for the same
    mask. This module never hand-rolls contouring; it only re-derives statistics over
    the components ``mask_to_polygons`` will polygonise anyway.
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
        area_km2 = float(np.sum(cell_area_m2) / 1.0e6)

        mean_sst = float(np.nanmean(sst[rows, cols]))
        mean_grad = float(np.nanmean(grad_km[rows, cols]))
        centroid_lat = float(np.mean(lats[rows]))
        centroid_lon = float(np.mean(lons[cols]))

        mean_chl: float | None = None
        chl_front_confirmed: bool | None = None
        if chl_on_sst is not None:
            cell_chl = chl_on_sst[rows, cols]
            if np.isfinite(cell_chl).any():
                mean_chl = float(np.nanmean(cell_chl))
            if chl_grad_on_sst is not None and chl_threshold is not None:
                cell_chl_grad = chl_grad_on_sst[rows, cols]
                chl_front_confirmed = bool(
                    np.isfinite(cell_chl_grad).any() and np.nanmax(cell_chl_grad) >= chl_threshold
                )

        records.append({
            "cell_count": int(count),
            "area_km2": area_km2,
            "mean_sst_degc": mean_sst,
            "front_strength_degc_per_km": mean_grad,
            "centroid_lat": centroid_lat,
            "centroid_lon": centroid_lon,
            "mean_chlorophyll_mg_m3": mean_chl,
            "chlorophyll_front_confirmed": chl_front_confirmed,
        })
    return records


@registry.tool(
    name="derive_pfz_zones",
    number=8,
    description=(
        "FORESHORE's own INDICATIVE derivation of likely fishing zones from "
        "sea-surface-temperature frontal gradients (and chlorophyll where a field for "
        "this basin and date exists), returned as polygons for display BESIDE the "
        "official INCOIS PFZ advisory. This is a derived product and must never be "
        "presented as the INCOIS advisory -- use find_nearest_pfz for that."
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
                    "Optional ISO-8601 target date/time. The OSF products run ~1-2 "
                    "days behind real time; the actual file date used is resolved from "
                    "the live catalogue (never assumed to be today) and reported in "
                    "the response's 'granule' field. Omit or null for the latest "
                    "available run."
                ),
            },
        },
        "required": [],
    },
    specialists=("OceanAnalytics",),
    reads_sources=("incois_osf_sst", "incois_osf_chl"),
    emits_derived=True,
    cost="slow",
)
def derive_pfz_zones(bbox: list[float] | None = None, when: str | None = None) -> ToolResult:
    """Derive indicative fishing-zone polygons from an INCOIS OSF SST frontal-gradient
    analysis (plus chlorophyll, when the field actually covers the bbox/date).

    Never raises: a missing SST input -- the one signal this derivation cannot run
    without -- degrades to ``ok=True, partial=True, missing=["incois_osf_sst"]`` with an
    abstaining summary, not an exception and not a fabricated zero-zone answer dressed
    up as success.
    """
    region = load_region()
    bbox_use = tuple(float(v) for v in bbox) if bbox else region.bbox
    when_dt, when_note = _parse_when(when)

    try:
        from ..sources.base import SourceError
        from ..sources.incois_thredds import IncoisThredds
    except Exception as exc:  # noqa: BLE001 -- a missing/broken adapter is an abstention here
        return ToolResult(
            tool="derive_pfz_zones",
            ok=True,
            partial=True,
            missing=["incois_osf_sst"],
            summary=(
                "FORESHORE-derived, INDICATIVE fishing-zone estimate -- not the "
                "official INCOIS PFZ advisory -- could not be computed: the INCOIS OSF "
                f"adapter failed to load ({type(exc).__name__}: {exc}). Abstaining "
                "rather than guessing."
            ),
            payload={
                "zones": _empty_feature_collection(),
                "method": {"description": "not computed -- adapter unavailable", "signals_used": []},
                "granule": None,
                "chlorophyll_available": False,
                "chlorophyll_reason": "not attempted -- adapter unavailable",
                "disclaimer": (
                    "This is a FORESHORE derivation, indicative only, and is never the "
                    "official INCOIS PFZ advisory."
                ),
            },
        )

    incois = IncoisThredds(region=region)

    try:
        sst_gs = incois.slice("sst", at=when_dt, bbox=bbox_use)
    except Exception as exc:  # noqa: BLE001 -- missing SST is an abstention, never a crash
        return ToolResult(
            tool="derive_pfz_zones",
            ok=True,
            partial=True,
            missing=["incois_osf_sst"],
            summary=(
                "FORESHORE-derived, INDICATIVE fishing-zone estimate -- not the "
                "official INCOIS PFZ advisory -- could not be computed: the INCOIS OSF "
                f"sea-surface-temperature feed is unavailable ({type(exc).__name__}: "
                f"{exc}). Abstaining rather than guessing; see find_nearest_pfz for the "
                "official advisory line in the meantime."
            ),
            payload={
                "zones": _empty_feature_collection(),
                "method": {"description": "not computed -- SST signal unavailable", "signals_used": []},
                "granule": None,
                "chlorophyll_available": False,
                "chlorophyll_reason": "not attempted -- SST signal unavailable",
                "disclaimer": (
                    "This is a FORESHORE derivation, indicative only, and is never the "
                    "official INCOIS PFZ advisory."
                ),
            },
        )

    sst = sst_gs.variables.get("sea_surface_temperature")
    if sst is None or sst.size == 0:
        return ToolResult(
            tool="derive_pfz_zones",
            ok=True,
            partial=True,
            missing=["incois_osf_sst"],
            summary=(
                "FORESHORE-derived, INDICATIVE fishing-zone estimate -- not the "
                "official INCOIS PFZ advisory -- could not be computed: the INCOIS OSF "
                f"SST grid for file date {sst_gs.file_date.isoformat()} carries no "
                "sea_surface_temperature values over the requested bbox. Abstaining "
                "rather than guessing."
            ),
            payload={
                "zones": _empty_feature_collection(),
                "method": {"description": "not computed -- SST grid empty over bbox", "signals_used": []},
                "granule": {
                    "product": "sst", "file_date": sst_gs.file_date.isoformat(),
                    "resolution_m": sst_gs.provenance.spatial_resolution_m, "model": sst_gs.history,
                },
                "chlorophyll_available": False,
                "chlorophyll_reason": "not attempted -- SST signal unavailable",
                "disclaimer": (
                    "This is a FORESHORE derivation, indicative only, and is never the "
                    "official INCOIS PFZ advisory."
                ),
            },
        )

    # -- signal 1 (required): SST frontal gradient ----------------------------------
    grad_km = _gradient_magnitude_per_km(sst, sst_gs.lats, sst_gs.lons)
    finite_grad = grad_km[np.isfinite(grad_km)]
    if finite_grad.size == 0:
        # Every cell is either land/fill or the grid was too small to differentiate --
        # zero zones is a valid, non-error outcome, not a failure to abstain over.
        threshold = None
        mask = np.zeros_like(grad_km, dtype=bool)
    else:
        threshold = float(np.percentile(finite_grad, GRADIENT_PERCENTILE))
        grad_grid = Grid(
            name="sst_gradient_magnitude", values=grad_km, lats=sst_gs.lats, lons=sst_gs.lons,
            times=None, unit="degC/km", attrs={},
        )
        mask = grad_grid.threshold_mask(">=", threshold)

    polygons: list[dict[str, Any]] = []
    if mask.any():
        grad_grid = Grid(
            name="sst_gradient_magnitude", values=grad_km, lats=sst_gs.lats, lons=sst_gs.lons,
            times=None, unit="degC/km", attrs={},
        )
        polygons = grad_grid.mask_to_polygons(mask, min_cells=MIN_CELLS_PER_ZONE)

    # -- signal 2 (opportunistic): chlorophyll, attempted fresh every call ----------
    chlorophyll_available = False
    chlorophyll_reason: str | None = None
    chl_gs = None
    chl_on_sst: np.ndarray | None = None
    chl_grad_on_sst: np.ndarray | None = None
    chl_threshold: float | None = None
    try:
        chl_gs = incois.slice("chl", at=when_dt, bbox=bbox_use)
        chl_arr = chl_gs.variables.get("chlorophyll_a")
        if chl_arr is not None and np.isfinite(chl_arr).any():
            chlorophyll_available = True
            chl_grad = _gradient_magnitude_per_km(chl_arr, chl_gs.lats, chl_gs.lons)
            chl_grid = Grid(
                name="chlorophyll_a", values=chl_arr, lats=chl_gs.lats, lons=chl_gs.lons,
                times=None, unit="mg/m^3", attrs={},
            )
            chl_grad_grid = Grid(
                name="chlorophyll_gradient_magnitude", values=chl_grad, lats=chl_gs.lats, lons=chl_gs.lons,
                times=None, unit="mg/m^3/km", attrs={},
            )
            chl_on_sst = regrid_to(chl_grid, sst_gs.lats, sst_gs.lons)
            chl_grad_on_sst = regrid_to(chl_grad_grid, sst_gs.lats, sst_gs.lons)
            finite_chl_grad = chl_grad_on_sst[np.isfinite(chl_grad_on_sst)]
            if finite_chl_grad.size:
                chl_threshold = float(np.percentile(finite_chl_grad, GRADIENT_PERCENTILE))
        else:
            chlorophyll_reason = (
                "the chlorophyll grid was fetched for this bbox/date but contains no "
                "valid (non-fill) cells"
            )
    except Exception as exc:  # noqa: BLE001 -- chlorophyll is a bonus signal, never required
        chlorophyll_reason = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, SourceError) and exc.status == 400:
            chlorophyll_reason += f" -- {_CHL_KNOWN_COVERAGE_NOTE}"

    # -- per-zone statistics + provenance --------------------------------------------
    records = _zone_records(
        mask, sst_gs.lats, sst_gs.lons, sst, grad_km, chl_on_sst, chl_grad_on_sst, chl_threshold,
    )

    prov_notes = [
        f"derived from INCOIS OSF sst granule, file date {sst_gs.file_date.isoformat()}",
    ]
    if sst_gs.history:
        prov_notes.append(f"model: {sst_gs.history}")
    prov_notes.append(f"gradient thresholded at the {GRADIENT_PERCENTILE:.0f}th percentile of the field itself")
    if chlorophyll_available and chl_gs is not None:
        prov_notes.append(
            f"cross-checked against INCOIS OSF chlorophyll granule, file date {chl_gs.file_date.isoformat()}"
        )
    prov = Provenance(
        source_id="foreshore_pfz_derived",
        source_name=(
            "FORESHORE derived PFZ estimate (SST frontal gradient"
            + (" + chlorophyll cross-check)" if chlorophyll_available else ")")
        ),
        authority="derived",
        url=sst_gs.provenance.url,
        acquired_at=sst_gs.provenance.acquired_at,
        issued_at=sst_gs.provenance.issued_at,
        valid_from=sst_gs.provenance.valid_from,
        valid_to=sst_gs.provenance.valid_to,
        spatial_resolution_m=sst_gs.provenance.spatial_resolution_m,
        is_derived=True,
        notes="; ".join(prov_notes),
    )

    observations: list[Observation] = []
    features: list[dict[str, Any]] = []
    for i, (geom, rec) in enumerate(zip(polygons, records)):
        zone_id = f"pfz_derived_{i + 1}"
        label = (
            "Indicative fishing-zone candidate (FORESHORE derived -- NOT the INCOIS "
            f"PFZ advisory): SST front, mean SST {rec['mean_sst_degc']:.2f} degC, "
            f"front strength {rec['front_strength_degc_per_km']:.2f} degC/km over "
            f"{rec['area_km2']:.1f} km^2"
        )
        if rec["chlorophyll_front_confirmed"] is not None:
            label += (
                "; chlorophyll cross-check "
                + ("confirmed" if rec["chlorophyll_front_confirmed"] else "not confirmed")
            )
        label += "."

        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "zone_id": zone_id,
                "is_derived": True,
                "label": label,
                "mean_sst_degc": rec["mean_sst_degc"],
                "front_strength_degc_per_km": rec["front_strength_degc_per_km"],
                "area_km2": rec["area_km2"],
                "cell_count": rec["cell_count"],
                "mean_chlorophyll_mg_m3": rec["mean_chlorophyll_mg_m3"],
                "chlorophyll_front_confirmed": rec["chlorophyll_front_confirmed"],
            },
        })

        for variable, value, unit in (
            ("derived_pfz_front_strength", rec["front_strength_degc_per_km"], "degC/km"),
            ("derived_pfz_mean_sst", rec["mean_sst_degc"], "degC"),
            ("derived_pfz_area", rec["area_km2"], "km^2"),
        ):
            observations.append(Observation(
                variable=variable,
                value=value,
                unit=unit,
                lat=rec["centroid_lat"],
                lon=rec["centroid_lon"],
                valid_time=sst_gs.valid_time,
                provenance=prov,
                qualifiers={"zone_id": zone_id, "cell_count": rec["cell_count"]},
            ))

    method_payload = {
        "description": (
            "SST gradient magnitude computed via numpy.gradient over the INCOIS OSF "
            "sst grid, converting degree spacing to true great-circle metres (with a "
            "cos(latitude) correction for the shrinking width of a degree of "
            f"longitude); thresholded at the {GRADIENT_PERCENTILE:.0f}th percentile of "
            "the gradient field itself (a relative, not absolute, cutoff so the "
            "method transfers across regions and seasons -- see module docstring); "
            "polygonised via Grid.mask_to_polygons, dropping components smaller than "
            f"{MIN_CELLS_PER_ZONE} connected 4-neighbour cells."
        ),
        "signals_used": ["incois_osf_sst"] + (["incois_osf_chl"] if chlorophyll_available else []),
        "signals_unavailable": [] if chlorophyll_available else ["incois_osf_chl"],
        "gradient_percentile": GRADIENT_PERCENTILE,
        "gradient_threshold_degc_per_km": threshold,
        "min_cells_per_zone": MIN_CELLS_PER_ZONE,
    }

    granule_payload = {
        "product": "sst",
        "file_date": sst_gs.file_date.isoformat(),
        "valid_time": sst_gs.valid_time.isoformat(),
        "resolution_m": sst_gs.provenance.spatial_resolution_m,
        "model": sst_gs.history,
        "chlorophyll_file_date": chl_gs.file_date.isoformat() if (chlorophyll_available and chl_gs is not None) else None,
    }

    if records:
        finding = f"{len(records)} candidate zone(s) identified from SST frontal structure"
    else:
        finding = "no SST front strong enough to clear the derivation threshold was found in the requested area"

    summary = (
        "FORESHORE-derived, INDICATIVE fishing-zone estimate -- NOT the official "
        "INCOIS PFZ advisory (use find_nearest_pfz for that) -- from the INCOIS OSF "
        f"sea-surface-temperature run for file date {sst_gs.file_date.isoformat()}: {finding}"
    )
    if chlorophyll_available:
        summary += "; cross-checked against the INCOIS OSF chlorophyll field for the same date."
    else:
        summary += f"; chlorophyll unavailable for this basin/date ({chlorophyll_reason}), derived on the SST front alone."
    if when_note:
        summary += f" ({when_note})"

    return ToolResult(
        tool="derive_pfz_zones",
        ok=True,
        observations=observations,
        payload={
            "zones": {"type": "FeatureCollection", "features": features},
            "method": method_payload,
            "granule": granule_payload,
            "chlorophyll_available": chlorophyll_available,
            "chlorophyll_reason": chlorophyll_reason,
            "disclaimer": (
                "This is a FORESHORE derivation, indicative only, computed from SST "
                "(and chlorophyll where available) frontal structure -- it is not the "
                "official INCOIS Potential Fishing Zone advisory."
            ),
        },
        summary=summary,
    )


__all__ = ["derive_pfz_zones"]
