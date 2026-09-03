"""The A* cost field — the weighted grid ``routing/astar.py`` searches over.

PLAN.md is explicit about why this file exists at all: *"Never LLM-generated waypoints —
a fake router is instantly visible to an ISRO judge."* Every number in :class:`CostField`
is either read straight off a config file (``config/routing.yaml``, ``config/vessels.yaml``)
or sampled from a real sourced grid/vector layer via :mod:`foreshore.store.grids` /
:mod:`foreshore.store.vectors`. Nothing here is a language-model guess.

Per-cell cost, exactly as specified in PLAN.md / CLAUDE.md::

    cost = w_base
         + w_hs      * (Hs / hs_max)^2
         + w_wind    * (wind / wind_max)^2
         + w_current * adverse_current_component      <- NOT baked into `cost`, see below
         + w_shallow * shallow_penalty(depth)
         + w_steep   * steepness_penalty(Hs, period)
         + w_imbl    * proximity_penalty(dist_to_IMBL)
         = INF  if land, inside/within the IMBL hard buffer, or inside an exclusion polygon

**Why the current term is *not* folded into ``CostField.cost``.** Every other term is a
property of the cell alone. The current term is not: "adverse" only has meaning relative
to a direction of travel, and the direction between two adjacent cells is not known until
A* actually considers that specific move. Baking a direction-free value into ``cost``
would either (a) always charge the worst case (a route that truly runs with the current
would still be billed as if it fought it — the whole point of modelling current would be
lost), or (b) always charge zero (silently discarding the ADVERSE, i.e. safety-relevant,
half of the signal). So ``CostField.cost`` deliberately holds the sum of every
*direction-independent* term (base/hs/wind/shallow/steep/imbl) plus the ``INF`` hard
blocks, and the current speed/direction grids are carried alongside it
(``current_speed_kn``, ``current_dir_deg``) so ``astar.py`` can compute the true adverse
component for each candidate move's actual bearing via :meth:`CostField.current_rate`.
``CostField.terms["current"]`` still exists (every term the contract asks for is
populated) but holds an *informational* worst-case value — "the penalty you would pay if
you had to steam straight into this cell's current" — used for the cost-field heatmap and
as the fallback :meth:`CostField.breakdown_at` value when no bearing is given; it is never
summed into ``cost`` and never charged to a route unless a leg's own bearing said so.

**Missing-input policy (invariant 3 — no unsourced numbers).** Every source this module
reads (INCOIS OSF wave/winds/currents, the ``bathymetry``/``coastline`` static layers) is
allowed to be unavailable. When a whole product cannot be fetched, its term is filled with
zeros and the canonical variable name is recorded in ``meta["missing"]`` — never silently
defaulted to a plausible-looking number. Cells where a fetched grid returned no data
(NaN — a genuine hole, e.g. the far side of a coverage boundary) get zero contribution
too, tracked per-variable in ``meta["coverage"]``.

**Formulas not given verbatim by PLAN.md/config/routing.yaml** — the top-level cost sum
and its two config-supplied ramps (``imbl.soft_buffer_nm``/``imbl.hard_buffer_nm`` and
``shallow.safety_factor``/``shallow.ramp_m``) are specified; the exact shape of
``shallow_penalty``/``proximity_penalty`` within those ramps is this module's own
documented interpretation, built only from those configured numbers (no invented
constant):

* ``proximity_penalty(dist)`` = 1.0 at/inside ``hard_buffer_nm`` (also the hard block),
  ramps linearly to 0.0 at ``soft_buffer_nm``, 0.0 beyond it.
* ``shallow_penalty(depth)`` = 1.0 (and a **hard block** — a vessel cannot float in less
  water than its own draft) below ``vessel.min_depth_m``; 1.0 (soft, not blocked) up to
  ``safety_factor * vessel.min_depth_m``; ramps linearly to 0.0 over the next
  ``shallow.ramp_m`` metres of depth; 0.0 beyond that.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any, Sequence

import numpy as np
import shapely
from shapely.ops import unary_union

from ..config import load_region, load_routing_config, load_vessels
from ..models import Observation, Provenance, haversine_nm, utcnow
from ..store.grids import Grid, regrid_to
from ..store.vectors import VectorStore

#: 1 m/s in knots — an SI unit-conversion identity, not a tunable parameter.
_MS_TO_KN = 1.9438444924406

_IMBL_LAYERS: tuple[str, ...] = ("imbl_historic_waters", "imbl_maritime_boundary")

#: Every non-cost-weight term this module derives, in the order the contract lists them.
_TERM_NAMES: tuple[str, ...] = ("base", "hs", "wind", "current", "shallow", "steep", "imbl")

#: Generous margin used only to size the *candidate search band* for the exact per-cell
#: IMBL distance measurement below — the same kind of safety multiplier
#: store/vectors.py already applies when widening its own STRtree candidate box (see its
#: ``_candidate_indices``). It is a search-performance detail, never a cost value.
_IMBL_BAND_MARGIN = 2.0
_IMBL_BAND_MIN_DEG = 0.02

BLOCKED_REASON_LEGEND: dict[str, str] = {
    "": "passable",
    "land": "on land (coastline polygon)",
    "imbl": "within the IMBL hard buffer",
    "exclusion": "inside a dynamic hazard-exclusion polygon (e.g. a cyclone cone)",
    "shallow": "shallower than the vessel's minimum operating depth",
}


def _cell_centers(vmin: float, vmax: float, step: float) -> np.ndarray:
    """Evenly spaced cell centres spanning ``[vmin, vmax]`` at approximately ``step``."""
    n = max(2, int(round((vmax - vmin) / step)))
    edges = np.linspace(vmin, vmax, n + 1)
    return (edges[:-1] + edges[1:]) / 2.0


def _static_layer_provenance(store: VectorStore, layer_id: str) -> Provenance:
    """Provenance for a static vector layer, mirroring the authority inference
    :mod:`foreshore.tools.geofence_tools` already uses for the same layers."""
    meta: dict[str, Any] = {}
    try:
        meta = store.layer_meta(layer_id) or {}
    except Exception:  # noqa: BLE001 — a missing/corrupt sidecar must not crash
        meta = {}
    acquired_raw = meta.get("acquired_at")
    acquired_at = (
        datetime.fromisoformat(acquired_raw) if isinstance(acquired_raw, str) else utcnow()
    )
    authority: Any = "derived"
    if layer_id.startswith("imbl"):
        authority = "VLIZ"
    elif layer_id == "bathymetry":
        authority = "INCOIS"
    return Provenance(
        source_id=meta.get("source_id", layer_id),
        source_name=f"FORESHORE static layer '{layer_id}'",
        authority=authority,
        url=f"local://static/{layer_id}.geojson",
        acquired_at=acquired_at,
        issued_at=acquired_at,
    )


def _grid_observation(
    variable: str, unit: str, grid2d: np.ndarray, centre_lat: float, centre_lon: float,
    provenance: Provenance | None,
) -> Observation | None:
    """One summary Observation per fetched grid variable — the mean, min and max actually
    used to build the cost field, with the exact Provenance the grid came from. A cost
    field samples thousands of cells; carrying one Observation per cell would swamp the
    evidence panel for no benefit, so this carries the grid's own statistics instead,
    which is what a reviewer actually wants to audit ("what wave field did this route use,
    and where did it come from")."""
    if provenance is None:
        return None
    valid = grid2d[~np.isnan(grid2d)]
    if valid.size == 0:
        return None
    return Observation(
        variable=variable,
        value=round(float(np.mean(valid)), 4),
        unit=unit,
        lat=centre_lat,
        lon=centre_lon,
        valid_time=provenance.issued_at or provenance.acquired_at,
        provenance=provenance,
        qualifiers={
            "grid_summary": True,
            "min": round(float(np.min(valid)), 4),
            "max": round(float(np.max(valid)), 4),
            "coverage_fraction": round(float(valid.size) / grid2d.size, 4),
            "shape": [int(x) for x in grid2d.shape],
        },
    )


def _land_mask(store: VectorStore, lat_grid: np.ndarray, lon_grid: np.ndarray) -> tuple[np.ndarray | None, str | None]:
    """Boolean land mask from the ``coastline`` static layer, or ``(None, note)`` when it
    is absent or not the polygon variant ``scripts/fetch_static.py`` writes when it
    succeeds (Natural Earth land polygons) — see that script's own fallback-(c) note about
    the line-only variant it writes when the polygon source is unreachable."""
    try:
        layers = store.layers()
    except Exception as exc:  # noqa: BLE001
        return None, f"could not list static layers: {type(exc).__name__}: {exc}"
    if "coastline" not in layers:
        return None, "coastline layer not fetched (run scripts/fetch_static.py); land is not masked"
    feats = store.read_layer("coastline")
    polys = [f.shape for f in feats if f.shape.geom_type in ("Polygon", "MultiPolygon")]
    if not polys:
        return None, "coastline layer present but holds no polygon geometry (line-only fallback); land is not masked"
    union = unary_union(polys)
    mask = shapely.contains_xy(union, lon_grid, lat_grid)
    return np.asarray(mask, dtype=bool), None


def _exclusion_mask(
    extra_exclusions: Sequence[dict] | None, lat_grid: np.ndarray, lon_grid: np.ndarray
) -> np.ndarray | None:
    if not extra_exclusions:
        return None
    geoms = []
    for geom in extra_exclusions:
        try:
            geoms.append(shapely.geometry.shape(geom))
        except Exception:  # noqa: BLE001 — one malformed geometry must not sink routing
            continue
    if not geoms:
        return None
    union = unary_union(geoms)
    return np.asarray(shapely.contains_xy(union, lon_grid, lat_grid), dtype=bool)


def _imbl_distance_grid(
    store: VectorStore, lats: np.ndarray, lons: np.ndarray, soft_nm: float
) -> tuple[np.ndarray, list[str]]:
    """Great-circle nm from every cell to the nearest IMBL line, via
    :meth:`VectorStore.nearest` — the exact haversine-based measurement the geofence
    engine itself uses, never reimplemented here.

    Computing that exact measurement for every cell in the full region grid would be
    tens of thousands of STRtree queries. Instead: cells whose (lat, lon) falls outside a
    generously padded bounding box of the IMBL geometry itself are certainly beyond
    ``soft_buffer_nm`` (the buffer is a few nautical miles; the padding here is far wider)
    and are left at the ``np.inf`` sentinel — correct, because ``inf`` produces exactly
    the same zero penalty a precisely-measured "very far away" would. Only the band of
    cells that could plausibly be within the buffer pays for an exact query.
    """
    nlat, nlon = lats.size, lons.size
    dist = np.full((nlat, nlon), np.inf)
    try:
        present = [lid for lid in _IMBL_LAYERS if lid in store.layers()]
    except Exception:  # noqa: BLE001
        present = []
    missing = [lid for lid in _IMBL_LAYERS if lid not in present]
    if not present:
        return dist, missing

    minx = miny = math.inf
    maxx = maxy = -math.inf
    for lid in present:
        for feat in store.read_layer(lid):
            b = feat.shape.bounds
            minx, miny = min(minx, b[0]), min(miny, b[1])
            maxx, maxy = max(maxx, b[2]), max(maxy, b[3])
    if not math.isfinite(minx):
        return dist, list(_IMBL_LAYERS)

    pad_deg = max((soft_nm / 60.0) * _IMBL_BAND_MARGIN, _IMBL_BAND_MIN_DEG)
    lat_idxs = np.nonzero((lats >= miny - pad_deg) & (lats <= maxy + pad_deg))[0]
    lon_idxs = np.nonzero((lons >= minx - pad_deg) & (lons <= maxx + pad_deg))[0]

    for i in lat_idxs:
        lat = float(lats[i])
        for j in lon_idxs:
            lon = float(lons[j])
            best = math.inf
            for lid in present:
                hits = store.nearest(lid, lat, lon, n=1)
                if hits and hits[0].distance_nm < best:
                    best = hits[0].distance_nm
            dist[i, j] = best
    return dist, missing


def _depth_grid(
    store: VectorStore, lat_grid: np.ndarray, lon_grid: np.ndarray
) -> tuple[np.ndarray, bool, str | None]:
    """Approximate depth (m) per cell: the depth attribute of the nearest bathymetry
    contour line. ``bathymetry`` is a set of depth-contour LineStrings
    (``PFZ_Bathymetry:bathymetry`` — see ``sources/incois_wfs.py``), not a raster, so
    "this cell's depth" is genuinely an approximation of the nearest charted contour, not
    an interpolated surface. Returns ``(depth_grid, missing, note)``.
    """
    shape = lat_grid.shape
    try:
        layers = store.layers()
    except Exception as exc:  # noqa: BLE001
        return np.full(shape, np.nan), True, f"could not list static layers: {exc}"
    if "bathymetry" not in layers:
        return np.full(shape, np.nan), True, "bathymetry layer not fetched (run scripts/fetch_static.py)"

    feats = store.read_layer("bathymetry")
    lines: list[Any] = []
    depths: list[float] = []
    for f in feats:
        raw = f.properties.get("BATHMETRY1", f.properties.get("BATHMETRY_"))
        if raw is None:
            continue
        try:
            depths.append(float(raw))
        except (TypeError, ValueError):
            continue
        lines.append(f.shape)
    if not lines:
        return np.full(shape, np.nan), True, "bathymetry layer present but no feature carried a usable depth attribute"

    try:
        from shapely.strtree import STRtree
        from shapely.geometry import Point

        tree = STRtree(lines)
        pts = [Point(lo, la) for lo, la in zip(lon_grid.ravel(), lat_grid.ravel())]
        pairs = tree.query_nearest(pts)
        nearest_for_point: dict[int, int] = {}
        for input_idx, tree_idx in zip(pairs[0].tolist(), pairs[1].tolist()):
            nearest_for_point.setdefault(input_idx, tree_idx)
        flat = np.full(len(pts), np.nan)
        for pt_idx, t_idx in nearest_for_point.items():
            flat[pt_idx] = depths[t_idx]
        return flat.reshape(shape), False, None
    except Exception as exc:  # noqa: BLE001 — a computation bug must degrade, not crash
        return np.full(shape, np.nan), True, f"bathymetry nearest-contour sampling failed: {type(exc).__name__}: {exc}"


def _proximity_penalty(dist_nm: np.ndarray, soft_nm: float, hard_nm: float) -> tuple[np.ndarray, np.ndarray]:
    """Soft ramp (1.0 at/inside ``hard_nm``, 0.0 at/beyond ``soft_nm``) plus the boolean
    hard-block mask. See module docstring for the documented ramp shape."""
    span = max(soft_nm - hard_nm, 1e-9)
    penalty = np.clip((soft_nm - dist_nm) / span, 0.0, 1.0)
    penalty = np.where(np.isfinite(dist_nm), penalty, 0.0)
    hard = np.isfinite(dist_nm) & (dist_nm <= hard_nm)
    return penalty, hard


def _shallow_penalty(depth: np.ndarray, min_depth_m: float, safety_factor: float, ramp_m: float) -> tuple[np.ndarray, np.ndarray]:
    """Soft ramp plus the "vessel literally cannot float here" hard-block mask. See
    module docstring for the documented ramp shape."""
    valid = ~np.isnan(depth)
    safe_depth = safety_factor * min_depth_m
    ramp = max(ramp_m, 1e-9)
    penalty = np.zeros_like(depth)
    with np.errstate(invalid="ignore"):
        penalty = np.where(depth < safe_depth, 1.0, np.clip((safe_depth + ramp - depth) / ramp, 0.0, 1.0))
    penalty = np.where(valid, penalty, 0.0)
    hard = valid & (depth < min_depth_m)
    return penalty, hard


@dataclass
class CostField:
    """The weighted routing grid, EPSG:4326, cell centres ascending in both axes."""

    lats: np.ndarray
    lons: np.ndarray
    cost: np.ndarray
    #: Per-term contribution grids: base, hs, wind, current, shallow, steep, imbl. Every
    #: term except "current" is baked into ``cost``; see the module docstring.
    terms: dict[str, np.ndarray]
    #: "" (passable) | "land" | "imbl" | "exclusion" | "shallow" per cell. See
    #: :data:`BLOCKED_REASON_LEGEND`.
    blocked_reason: np.ndarray
    provenance: list[Provenance]
    evidence: list[Observation]
    meta: dict[str, Any]
    #: Direction-dependent current fields astar.py needs to compute the adverse
    #: component for a specific move's bearing. NaN where current data was unavailable.
    current_speed_kn: np.ndarray
    #: Compass bearing (0=N, clockwise) the current flows *toward*.
    current_dir_deg: np.ndarray

    # -- grid <-> lat/lon -----------------------------------------------------------

    def index_of(self, lat: float, lon: float) -> tuple[int, int]:
        step_lat = (self.lats[-1] - self.lats[0]) / (self.lats.size - 1) if self.lats.size > 1 else 1.0
        step_lon = (self.lons[-1] - self.lons[0]) / (self.lons.size - 1) if self.lons.size > 1 else 1.0
        i = int(round((lat - self.lats[0]) / step_lat)) if step_lat else 0
        j = int(round((lon - self.lons[0]) / step_lon)) if step_lon else 0
        i = min(max(i, 0), self.lats.size - 1)
        j = min(max(j, 0), self.lons.size - 1)
        return i, j

    def latlon_of(self, i: int, j: int) -> tuple[float, float]:
        return float(self.lats[i]), float(self.lons[j])

    # -- explainability ---------------------------------------------------------------

    def current_rate(self, i: int, j: int, bearing_deg: float) -> float:
        """The true adverse-current cost *rate* for a move through cell ``(i, j)`` on
        ``bearing_deg`` — ``w_current * max(0, -component) / current_max_kn``, where
        ``component`` is the current's projection onto the direction of travel. A
        following current projects positively and is clamped to zero cost, never a
        bonus; this module only ever penalises, per the given cost formula."""
        speed = float(self.current_speed_kn[i, j])
        direction = float(self.current_dir_deg[i, j])
        if math.isnan(speed) or math.isnan(direction):
            return 0.0
        w_current = float(self.meta["weights"].get("current", 0.0))
        current_max = self.meta["normalisers"].get("current_max_kn")
        if not current_max:
            return 0.0
        along = speed * math.cos(math.radians(direction - bearing_deg))
        adverse = max(0.0, -along)
        return w_current * (adverse / float(current_max))

    def breakdown_at(self, i: int, j: int, bearing_deg: float | None = None) -> dict[str, float]:
        """Per-term cost contribution at one cell. ``bearing_deg`` is optional: when
        given, "current" is the true direction-aware value from :meth:`current_rate`;
        omitted, it falls back to the informational worst-case grid in
        ``terms["current"]`` (documented in the module docstring)."""
        out = {name: float(self.terms[name][i, j]) for name in _TERM_NAMES}
        if bearing_deg is not None:
            out["current"] = self.current_rate(i, j, bearing_deg)
        return out


def _build_cost_field_uncached(
    bbox: tuple[float, float, float, float] | None = None,
    *,
    when: datetime | None = None,
    vessel_class_id: str | None = None,
    region_id: str | None = None,
    extra_exclusions: Sequence[dict] | None = None,
) -> CostField:
    """The real cost-field build -- INCOIS OSF wave/wind/current fetch + regrid, IMBL
    proximity sampling, bathymetry sampling, all of it, every time it is called. Measured
    at ~11 s for the demo region, dominated by :func:`_imbl_distance_grid`'s per-cell
    IMBL-proximity sampling and the wave/wind grid regridding over a ~260x290 cell grid.

    Not meant to be called directly outside this module -- :func:`build_cost_field` is
    the public entry point and puts a small bounded cache in front of this for the common
    (whole-region, no dynamic exclusions) case. Kept as a separate function, rather than
    inlined, purely so the cache wrapper below has something precise to memoize and a
    test has something precise to count calls to."""
    region = load_region(region_id)
    routing_cfg = load_routing_config()
    vessel = load_vessels().get(vessel_class_id)
    store = VectorStore()

    bbox_used = tuple(float(v) for v in bbox) if bbox else region.bbox
    minlon, minlat, maxlon, maxlat = bbox_used
    when = when or utcnow()

    lats = _cell_centers(minlat, maxlat, routing_cfg.grid_deg)
    lons = _cell_centers(minlon, maxlon, routing_cfg.grid_deg)
    nlat, nlon = lats.size, lons.size
    lon_grid, lat_grid = np.meshgrid(lons, lats)
    centre_lat, centre_lon = (minlat + maxlat) / 2.0, (minlon + maxlon) / 2.0

    weights = routing_cfg.weights
    norms = routing_cfg.normalisers
    imbl_cfg = routing_cfg.imbl
    shallow_cfg = routing_cfg.shallow

    missing: list[str] = []
    coverage: dict[str, float] = {}
    evidence: list[Observation] = []
    provenance: list[Provenance] = []

    # -- INCOIS OSF wave / winds / currents, regridded onto the routing grid -----------
    hs_grid = np.full((nlat, nlon), np.nan)
    period_grid = np.full((nlat, nlon), np.nan)
    wind_grid = np.full((nlat, nlon), np.nan)
    current_speed_kn = np.full((nlat, nlon), np.nan)
    current_dir_deg = np.full((nlat, nlon), np.nan)

    try:
        from ..sources.incois_thredds import IncoisThredds, UNITS as _OSF_UNITS

        thredds = IncoisThredds(region=region)

        def _fetch(product: str, canon_vars: tuple[str, ...]) -> None:
            try:
                gs = thredds.slice(product, variables=list(canon_vars), at=when, bbox=bbox_used)
            except Exception as exc:  # noqa: BLE001 — one product's outage is not fatal
                missing.extend(canon_vars)
                return
            provenance.append(gs.provenance)
            for canon in canon_vars:
                raw = gs.variables.get(canon)
                if raw is None:
                    missing.append(canon)
                    continue
                g = Grid(
                    name=canon, values=raw, lats=gs.lats, lons=gs.lons, times=None,
                    unit=_OSF_UNITS.get(canon, ""), attrs={},
                )
                regridded = regrid_to(g, lats, lons)
                coverage[canon] = round(float(np.mean(~np.isnan(regridded))), 4)
                obs = _grid_observation(canon, _OSF_UNITS.get(canon, ""), regridded, centre_lat, centre_lon, gs.provenance)
                if obs is not None:
                    evidence.append(obs)
                if canon == "significant_wave_height":
                    hs_grid[:] = regridded
                elif canon == "wave_period":
                    period_grid[:] = regridded
                elif canon == "wind_speed":
                    wind_grid[:] = regridded * _MS_TO_KN
                elif canon == "current_speed":
                    current_speed_kn[:] = regridded * _MS_TO_KN
                elif canon == "current_direction":
                    current_dir_deg[:] = regridded

        _fetch("wave", ("significant_wave_height", "wave_period"))
        _fetch("winds", ("wind_speed",))
        _fetch("currents", ("current_speed", "current_direction"))
    except Exception as exc:  # noqa: BLE001 — adapter module itself unavailable
        missing.extend([
            "significant_wave_height", "wave_period", "wind_speed",
            "current_speed", "current_direction",
        ])

    # -- derived wave steepness, reusing verdict.engine.steepness cell-by-cell --------
    from ..verdict.engine import steepness as _steepness_fn

    steep_grid = np.full((nlat, nlon), np.nan)
    for i in range(nlat):
        for j in range(nlon):
            h = hs_grid[i, j]
            p = period_grid[i, j]
            if math.isnan(h) or math.isnan(p):
                continue
            s = _steepness_fn(float(h), float(p))
            if s is not None:
                steep_grid[i, j] = s

    # -- static layers: land mask, IMBL distance, bathymetry --------------------------
    land_mask, land_note = _land_mask(store, lat_grid, lon_grid)
    if land_mask is None:
        missing.append("coastline")

    dist_imbl, imbl_missing = _imbl_distance_grid(store, lats, lons, imbl_cfg.get("soft_buffer_nm", 3.0))
    if imbl_missing:
        missing.append("static_imbl")
    else:
        for lid in _IMBL_LAYERS:
            provenance.append(_static_layer_provenance(store, lid))

    depth_grid, depth_missing, depth_note = _depth_grid(store, lat_grid, lon_grid)
    if depth_missing:
        missing.append("bathymetry")
    else:
        provenance.append(_static_layer_provenance(store, "bathymetry"))

    exclusion_mask = _exclusion_mask(extra_exclusions, lat_grid, lon_grid)

    # -- term grids ---------------------------------------------------------------------
    base_term = np.full((nlat, nlon), float(weights.get("base", 0.0)))

    hs_max = norms.get("hs_max_m")
    hs_term = np.zeros((nlat, nlon))
    if hs_max:
        valid = ~np.isnan(hs_grid)
        hs_term[valid] = weights.get("hs", 0.0) * (hs_grid[valid] / hs_max) ** 2

    wind_max = norms.get("wind_max_kn")
    wind_term = np.zeros((nlat, nlon))
    if wind_max:
        valid = ~np.isnan(wind_grid)
        wind_term[valid] = weights.get("wind", 0.0) * (wind_grid[valid] / wind_max) ** 2

    steep_max = norms.get("steepness_max")
    steep_term = np.zeros((nlat, nlon))
    if steep_max:
        valid = ~np.isnan(steep_grid)
        steep_term[valid] = weights.get("steep", 0.0) * (steep_grid[valid] / steep_max)

    current_max = norms.get("current_max_kn")
    current_term = np.zeros((nlat, nlon))
    if current_max:
        valid = ~np.isnan(current_speed_kn)
        # Informational worst-case (see module docstring): as if the cell's full current
        # speed opposed travel head-on. Never summed into `cost`.
        current_term[valid] = weights.get("current", 0.0) * (current_speed_kn[valid] / current_max)

    shallow_penalty, shallow_hard = _shallow_penalty(
        depth_grid, vessel.min_depth_m, shallow_cfg.get("safety_factor", 1.0), shallow_cfg.get("ramp_m", 1.0)
    )
    shallow_term = weights.get("shallow", 0.0) * shallow_penalty

    imbl_penalty, imbl_hard = _proximity_penalty(
        dist_imbl, imbl_cfg.get("soft_buffer_nm", 3.0), imbl_cfg.get("hard_buffer_nm", 0.3)
    )
    imbl_term = weights.get("imbl", 0.0) * imbl_penalty

    cost = base_term + hs_term + wind_term + shallow_term + steep_term + imbl_term

    blocked_reason = np.full((nlat, nlon), "", dtype=object)
    blocked_reason[shallow_hard] = "shallow"
    blocked_reason[imbl_hard] = "imbl"
    if exclusion_mask is not None:
        blocked_reason[exclusion_mask] = "exclusion"
    if land_mask is not None:
        blocked_reason[land_mask] = "land"
    cost = np.where(blocked_reason != "", np.inf, cost)

    finite = cost[np.isfinite(cost)]
    min_finite_cost = float(finite.min()) if finite.size else None

    terms = {
        "base": base_term, "hs": hs_term, "wind": wind_term, "current": current_term,
        "shallow": shallow_term, "steep": steep_term, "imbl": imbl_term,
    }

    notes = [n for n in (land_note, depth_note) if n]
    meta: dict[str, Any] = {
        "grid_deg": routing_cfg.grid_deg,
        "bbox": bbox_used,
        "departure": when.isoformat(),
        "vessel_class": vessel.class_id,
        "region_id": region.region_id,
        "weights": dict(weights),
        "normalisers": dict(norms),
        "imbl": dict(imbl_cfg),
        "shallow": dict(shallow_cfg),
        "heuristic": dict(routing_cfg.heuristic),
        "missing": sorted(set(missing)),
        "coverage": coverage,
        "notes": notes,
        "min_finite_cost": min_finite_cost,
        "grid_shape": [nlat, nlon],
        "blocked_reason_legend": dict(BLOCKED_REASON_LEGEND),
        "blocked_cell_count": int((blocked_reason != "").sum()),
    }

    return CostField(
        lats=lats, lons=lons, cost=cost, terms=terms, blocked_reason=blocked_reason,
        provenance=provenance, evidence=evidence, meta=meta,
        current_speed_kn=current_speed_kn, current_dir_deg=current_dir_deg,
    )


#: In-process cache in front of :func:`_build_cost_field_uncached`, keyed on exactly the
#: three things the built field actually depends on for the common (whole-region, no
#: dynamic exclusions) call shape: the *active* region id, the resolved vessel class id,
#: and ``when`` rounded down to the start of its containing hour. ``maxsize=16`` bounds
#: memory for a long-running demo process -- one region x a couple of vessel classes x a
#: handful of hours already comfortably covers a real rehearsal or live-demo session, and
#: an evicted/cold combination just falls back to a normal (slow) rebuild, never an error.
#:
#: Hourly bucketing is deliberately coarser than anything driving a stale-data concern:
#: INCOIS OSF wave/wind/current grids lag ~2 days and the IMD bulletin/static layers
#: (bathymetry, coastline, IMBL) change less often than that, so reusing a build from
#: earlier in the same hour cannot surface data staler than the source data already is --
#: it only saves the ~11 s of local recomputation over already-fetched/cached source data
#: (network-level caching for the sources themselves lives separately in
#: ``store/cache.py`` and is untouched by this).
@lru_cache(maxsize=16)
def _build_cost_field_cached(region_id: str, vessel_class_id: str, when_bucket: datetime) -> CostField:
    return _build_cost_field_uncached(
        None, when=when_bucket, vessel_class_id=vessel_class_id, region_id=region_id,
        extra_exclusions=None,
    )


def build_cost_field(
    bbox: tuple[float, float, float, float] | None = None,
    *,
    when: datetime | None = None,
    vessel_class_id: str | None = None,
    region_id: str | None = None,
    extra_exclusions: Sequence[dict] | None = None,
) -> CostField:
    """Build (or reuse a cached) A* routing cost field.

    ``_build_cost_field_uncached`` is a pure function of (the active region, the vessel
    class, the ``when`` instant it samples time-varying grids for) given whatever source
    data is available at call time -- so for the common case this codebase actually
    calls it with (whole-region ``bbox``, i.e. ``None``, and no ``extra_exclusions`` --
    true for both ``tools/routing_tools.py``'s ``plan_route`` and
    ``tests/test_astar.py``), the result is safe to compute once per (region, vessel
    class, hour) and reuse. This resolves the *active* region id and vessel class id the
    exact same way ``_build_cost_field_uncached`` itself would (``load_region``/
    ``load_vessels`` -- no new global invented) and serves the result from
    :func:`_build_cost_field_cached`.

    A caller-supplied ``bbox`` or ``extra_exclusions`` (e.g. a dynamic hazard polygon)
    bypasses the cache entirely and always builds fresh: neither is part of the cache
    key, so caching them under the same (region, vessel, hour) key would risk silently
    returning a field for the wrong area or missing a hazard polygon it was never asked
    to account for.
    """
    if bbox is None and extra_exclusions is None:
        region = load_region(region_id)
        vessel = load_vessels().get(vessel_class_id)
        when_resolved = when or utcnow()
        when_bucket = when_resolved.replace(minute=0, second=0, microsecond=0)
        return _build_cost_field_cached(region.region_id, vessel.class_id, when_bucket)
    return _build_cost_field_uncached(
        bbox, when=when, vessel_class_id=vessel_class_id, region_id=region_id,
        extra_exclusions=extra_exclusions,
    )


__all__ = ["CostField", "build_cost_field", "BLOCKED_REASON_LEGEND"]
