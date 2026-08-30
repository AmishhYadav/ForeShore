"""Gridded (NetCDF/array) data store — waves, wind, currents, SST, chlorophyll and the
routing cost field all flow through :class:`Grid`.

This module works on files and in-memory arrays only. It has no knowledge of any
particular data source (INCOIS THREDDS, Open-Meteo, ...) — adapters under
``foreshore.sources`` build :class:`Grid` instances and call :func:`open_netcdf`; this
module never imports back the other way.

Geometry rules the routing cost field and hazard-exclusion polygons rely on:

- ``at()`` reports distance in true great-circle metres
  (:func:`~foreshore.models.haversine_m`), never a degree-space approximation.
- Latitude and longitude axes may be ascending or descending; every lookup handles both
  without the caller needing to know which.
- A missing value is ``NaN`` in the underlying array and ``None`` at the public API
  boundary — the LLM-facing layer never sees a float that means "no data".
- :meth:`Grid.mask_to_polygons` builds a connected-component analysis with plain numpy
  (no scipy dependency) and turns each surviving component into the union of its cell
  rectangles via shapely. Correctness — a polygon that actually covers the flagged
  cells — matters more than a smooth coastline here; this is used for high-wave
  exclusion zones and derived PFZ candidates, never presented as an official boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import orjson
import xarray as xr
from shapely.geometry import box as shapely_box, mapping as shapely_mapping
from shapely.ops import unary_union

from ..models import UTC, haversine_m, utcnow

_COMPARATORS = {
    ">": np.greater,
    ">=": np.greater_equal,
    "<": np.less,
    "<=": np.less_equal,
}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _nearest_indices(axis: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """Vectorised nearest-index lookup along a 1D coordinate axis.

    Handles an axis that is sorted ascending or descending (INCOIS/IMD grids are not
    consistent about this). Uses ``np.searchsorted`` on the ascending view, never a
    Python loop, so it stays cheap when called once per cell of a ~260 x 290 routing
    grid.
    """
    axis = np.asarray(axis, dtype=float)
    targets = np.asarray(targets, dtype=float)
    n = axis.shape[0]
    if n == 1:
        return np.zeros(targets.shape, dtype=int)
    ascending = axis[-1] >= axis[0]
    a = axis if ascending else axis[::-1]
    idx = np.searchsorted(a, targets, side="left")
    idx = np.clip(idx, 1, n - 1)
    left = a[idx - 1]
    right = a[idx]
    idx = np.where(np.abs(targets - left) <= np.abs(right - targets), idx - 1, idx)
    idx = np.clip(idx, 0, n - 1)
    if not ascending:
        idx = (n - 1) - idx
    return idx.astype(int)


def _label_components(mask: np.ndarray) -> np.ndarray:
    """4-connected connected-component labelling using plain numpy — no scipy.

    Every ``True`` cell starts labelled with its own flat index; each iteration relaxes
    every cell's label to the minimum label among itself and its True 4-neighbours.
    Labels only ever decrease and are bounded below by zero, so this is guaranteed to
    reach a fixed point — the point at which every cell's label equals the smallest
    flat index anywhere in its connected component — in a finite number of steps.
    ``False`` cells keep label ``-1`` throughout.
    """
    h, w = mask.shape
    labels = np.where(mask, np.arange(h * w, dtype=np.int64).reshape(h, w), -1)
    if not mask.any():
        return labels
    while True:
        new = labels.copy()
        # up
        cand = np.minimum(new[1:, :], labels[:-1, :])
        new[1:, :] = np.where(mask[1:, :] & (labels[:-1, :] >= 0), cand, new[1:, :])
        # down
        cand = np.minimum(new[:-1, :], labels[1:, :])
        new[:-1, :] = np.where(mask[:-1, :] & (labels[1:, :] >= 0), cand, new[:-1, :])
        # left
        cand = np.minimum(new[:, 1:], labels[:, :-1])
        new[:, 1:] = np.where(mask[:, 1:] & (labels[:, :-1] >= 0), cand, new[:, 1:])
        # right
        cand = np.minimum(new[:, :-1], labels[:, 1:])
        new[:, :-1] = np.where(mask[:, :-1] & (labels[:, 1:] >= 0), cand, new[:, :-1])
        if np.array_equal(new, labels):
            return new
        labels = new


def _cell_edges(centers: np.ndarray) -> np.ndarray:
    """Cell boundary coordinates along one axis (``len(centers) + 1`` edges).

    Interior edges are midpoints between consecutive centres; the two outer edges are
    extrapolated by the same gap as their neighbouring interval. Works for either
    ascending or descending ``centers`` — the edges simply inherit that direction.
    """
    centers = np.asarray(centers, dtype=float)
    n = centers.size
    if n == 1:
        return np.array([centers[0] - 0.5, centers[0] + 0.5])
    mids = (centers[:-1] + centers[1:]) / 2.0
    first_gap = mids[0] - centers[0]
    last_gap = centers[-1] - mids[-1]
    edges = np.empty(n + 1, dtype=float)
    edges[0] = centers[0] - first_gap
    edges[1:-1] = mids
    edges[-1] = centers[-1] + last_gap
    return edges


def _to_geojson(geom: Any) -> dict[str, Any]:
    """shapely geometry -> plain-Python GeoJSON dict (tuples -> lists) via an orjson
    round trip, so downstream code never has to special-case tuples vs. lists."""
    return orjson.loads(orjson.dumps(shapely_mapping(geom)))


@dataclass(frozen=True)
class Grid:
    """A single named variable on a lat/lon grid, optionally with a time axis.

    ``values`` is ``(lat, lon)`` for a static field or ``(time, lat, lon)`` for a time
    series; which one is inferred from ``values.ndim``, not carried as a separate flag.
    """

    name: str
    values: np.ndarray
    lats: np.ndarray
    lons: np.ndarray
    times: list[datetime] | None
    unit: str
    attrs: dict[str, Any]

    def __post_init__(self) -> None:
        lats = np.asarray(self.lats, dtype=float)
        lons = np.asarray(self.lons, dtype=float)
        values = np.asarray(self.values, dtype=float)
        object.__setattr__(self, "lats", lats)
        object.__setattr__(self, "lons", lons)
        object.__setattr__(self, "values", values)
        if values.ndim not in (2, 3):
            raise ValueError(f"Grid {self.name!r}: values must be 2D or 3D, got shape {values.shape}")
        if values.shape[-2:] != (lats.size, lons.size):
            raise ValueError(
                f"Grid {self.name!r}: values shape {values.shape} does not match "
                f"lats({lats.size}) x lons({lons.size})"
            )
        if values.ndim == 3 and self.times is not None and len(self.times) != values.shape[0]:
            raise ValueError(
                f"Grid {self.name!r}: {len(self.times)} times but {values.shape[0]} time steps"
            )

    # -- time -----------------------------------------------------------------------------

    def _time_index(self, when: datetime | None) -> int:
        if self.values.ndim != 3 or not self.times:
            return 0
        target = _aware(when) if when is not None else utcnow()
        diffs = [abs((_aware(t) - target).total_seconds()) for t in self.times]
        return int(np.argmin(diffs))

    def to_array(self, when: datetime | None = None) -> np.ndarray:
        """2D slice at the nearest time step (a no-op for an already-2D grid)."""
        if self.values.ndim == 2:
            return self.values
        return self.values[self._time_index(when)]

    # -- point lookups ----------------------------------------------------------------------

    def at(self, lat: float, lon: float, when: datetime | None = None) -> tuple[float | None, float]:
        """Nearest-cell value plus great-circle distance in metres to that cell centre.

        Returns ``(None, distance_m)`` when the nearest cell is ``NaN`` — a missing
        observation is a fact worth returning, never silently coerced to zero.
        """
        lat_idx = int(_nearest_indices(self.lats, np.array([lat]))[0])
        lon_idx = int(_nearest_indices(self.lons, np.array([lon]))[0])
        arr = self.to_array(when)
        raw = float(arr[lat_idx, lon_idx])
        value = None if np.isnan(raw) else raw
        dist_m = haversine_m(lat, lon, float(self.lats[lat_idx]), float(self.lons[lon_idx]))
        return value, dist_m

    def bilinear(self, lat: float, lon: float, when: datetime | None = None) -> float | None:
        """Bilinear interpolation among the 4 surrounding cells; queries outside the
        grid bounds are clamped to the edge. Falls back to the nearest-cell value when
        one or more of the 4 neighbours is ``NaN``, rather than propagating a NaN."""
        arr = self.to_array(when)
        lats, lons = self.lats, self.lons
        if lats.size < 2 or lons.size < 2:
            return self.at(lat, lon, when)[0]

        lat_asc = lats[-1] >= lats[0]
        lon_asc = lons[-1] >= lons[0]
        lats_a = lats if lat_asc else lats[::-1]
        lons_a = lons if lon_asc else lons[::-1]
        arr_a = arr if lat_asc else arr[::-1, :]
        arr_a = arr_a if lon_asc else arr_a[:, ::-1]

        lat_c = min(max(lat, float(lats_a[0])), float(lats_a[-1]))
        lon_c = min(max(lon, float(lons_a[0])), float(lons_a[-1]))

        i1 = int(np.clip(np.searchsorted(lats_a, lat_c, side="right"), 1, lats_a.size - 1))
        i0 = i1 - 1
        j1 = int(np.clip(np.searchsorted(lons_a, lon_c, side="right"), 1, lons_a.size - 1))
        j0 = j1 - 1

        lat0, lat1v = float(lats_a[i0]), float(lats_a[i1])
        lon0, lon1v = float(lons_a[j0]), float(lons_a[j1])
        tlat = 0.0 if lat1v == lat0 else (lat_c - lat0) / (lat1v - lat0)
        tlon = 0.0 if lon1v == lon0 else (lon_c - lon0) / (lon1v - lon0)

        v00, v01 = float(arr_a[i0, j0]), float(arr_a[i0, j1])
        v10, v11 = float(arr_a[i1, j0]), float(arr_a[i1, j1])
        if any(np.isnan(v) for v in (v00, v01, v10, v11)):
            return self.at(lat, lon, when)[0]
        v0 = v00 * (1 - tlon) + v01 * tlon
        v1 = v10 * (1 - tlon) + v11 * tlon
        return v0 * (1 - tlat) + v1 * tlat

    # -- spatial ops --------------------------------------------------------------------------

    def subset(self, bbox: tuple[float, float, float, float]) -> "Grid":
        """Crop to ``bbox = (minlon, minlat, maxlon, maxlat)``. Preserves axis order
        (ascending or descending) and the full time axis untouched."""
        minlon, minlat, maxlon, maxlat = bbox
        lat_idx = np.nonzero((self.lats >= minlat) & (self.lats <= maxlat))[0]
        lon_idx = np.nonzero((self.lons >= minlon) & (self.lons <= maxlon))[0]
        if self.values.ndim == 3:
            t_idx = np.arange(self.values.shape[0])
            new_values = self.values[np.ix_(t_idx, lat_idx, lon_idx)]
        else:
            new_values = self.values[np.ix_(lat_idx, lon_idx)]
        return Grid(
            name=self.name,
            values=new_values,
            lats=self.lats[lat_idx],
            lons=self.lons[lon_idx],
            times=self.times,
            unit=self.unit,
            attrs=dict(self.attrs),
        )

    def threshold_mask(self, op: str, value: float, when: datetime | None = None) -> np.ndarray:
        """Boolean 2D mask; ``NaN`` cells are always ``False`` regardless of ``op``."""
        if op not in _COMPARATORS:
            raise ValueError(f"unsupported comparator {op!r}; expected one of {sorted(_COMPARATORS)}")
        arr = self.to_array(when)
        with np.errstate(invalid="ignore"):
            mask = _COMPARATORS[op](arr, value)
        return np.where(np.isnan(arr), False, mask).astype(bool)

    def mask_to_polygons(self, mask: np.ndarray, *, min_cells: int = 4) -> list[dict[str, Any]]:
        """One GeoJSON ``Polygon`` geometry per 4-connected component of ``mask`` with
        at least ``min_cells`` True cells. Each component is emitted as the shapely
        union of its cell rectangles — a rectilinear boundary, not a smoothed one; that
        is a deliberate tradeoff for correctness over cosmetics."""
        mask = np.asarray(mask, dtype=bool)
        if mask.shape != (self.lats.size, self.lons.size):
            raise ValueError(
                f"mask shape {mask.shape} does not match grid ({self.lats.size}, {self.lons.size})"
            )
        if not mask.any():
            return []
        labels = _label_components(mask)
        lat_edges = _cell_edges(self.lats)
        lon_edges = _cell_edges(self.lons)

        polygons: list[dict[str, Any]] = []
        flat_labels = labels[labels >= 0]
        unique_labels, counts = np.unique(flat_labels, return_counts=True)
        for lbl, count in zip(unique_labels.tolist(), counts.tolist()):
            if count < min_cells:
                continue
            rows, cols = np.nonzero(labels == lbl)
            boxes = []
            for r, c in zip(rows.tolist(), cols.tolist()):
                lat0, lat1 = sorted((lat_edges[r], lat_edges[r + 1]))
                lon0, lon1 = sorted((lon_edges[c], lon_edges[c + 1]))
                boxes.append(shapely_box(lon0, lat0, lon1, lat1))
            merged = unary_union(boxes)
            geoms = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
            polygons.extend(_to_geojson(g) for g in geoms)
        return polygons


# --------------------------------------------------------------------------------------
# NetCDF ingestion
# --------------------------------------------------------------------------------------


def _find_coord(ds: xr.Dataset, candidates: tuple[str, ...], *, required: bool = True) -> str | None:
    names = set(ds.variables) | set(ds.dims)
    lower_map = {str(n).lower(): str(n) for n in names}
    for cand in candidates:
        hit = lower_map.get(cand.lower())
        if hit is not None:
            return hit
    if required:
        raise ValueError(f"none of {candidates} found among dataset dims/variables {sorted(map(str, names))}")
    return None


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, np.datetime64):
        seconds = value.astype("datetime64[s]").astype(int)
        return datetime.fromtimestamp(int(seconds), tz=timezone.utc)
    if hasattr(value, "isoformat"):
        return _aware(datetime.fromisoformat(value.isoformat()))
    raise TypeError(f"cannot convert {value!r} of type {type(value)} to datetime")


def open_netcdf(path: Path, var_map: dict[str, str] | None = None) -> dict[str, Grid]:
    """Open a NetCDF file and return ``canonical_name -> Grid`` for every data variable
    defined over the detected lat/lon axes (optionally a time axis too).

    ``var_map`` renames file variable names to canonical ones (e.g. ``SWH`` ->
    ``significant_wave_height``); anything not listed keeps its file name. Coordinate
    names are detected heuristically (case-insensitively) among
    ``lat/latitude/y/LAT``, ``lon/longitude/x/LON``, ``time/Time/TIME``. Size-1
    dimensions other than lat/lon/time (a depth level, an ensemble member of size 1,
    ...) are squeezed away; a variable that still carries an extra non-degenerate
    dimension after squeezing is skipped, since :class:`Grid` only models 2D/3D data.
    """
    var_map = dict(var_map or {})
    ds = xr.open_dataset(path, decode_times=True)
    try:
        lat_name = _find_coord(ds, ("lat", "latitude", "y", "LAT"))
        lon_name = _find_coord(ds, ("lon", "longitude", "x", "LON"))
        time_name = _find_coord(ds, ("time", "Time", "TIME"), required=False)

        lats = np.asarray(ds[lat_name].values, dtype=float)
        lons = np.asarray(ds[lon_name].values, dtype=float)
        times: list[datetime] | None = None
        if time_name is not None and time_name in ds.variables:
            times = [_to_datetime(t) for t in ds[time_name].values]

        protect = {lat_name, lon_name} | ({time_name} if time_name else set())
        grids: dict[str, Grid] = {}
        for var_name, da in ds.data_vars.items():
            if str(var_name) in protect:
                continue
            squeeze_dims = [d for d in da.dims if d not in protect and da.sizes[d] == 1]
            if squeeze_dims:
                da = da.squeeze(dim=squeeze_dims, drop=True)
            if lat_name not in da.dims or lon_name not in da.dims:
                continue
            if set(da.dims) - protect:
                continue  # a non-degenerate extra dimension Grid does not model
            has_time = bool(time_name) and time_name in da.dims
            order = ([time_name] if has_time else []) + [lat_name, lon_name]
            da = da.transpose(*order)
            canonical = var_map.get(str(var_name), str(var_name))
            grids[canonical] = Grid(
                name=canonical,
                values=np.asarray(da.values, dtype=float),
                lats=lats,
                lons=lons,
                times=times if has_time else None,
                unit=str(da.attrs.get("units", da.attrs.get("unit", ""))),
                attrs=dict(da.attrs),
            )
        return grids
    finally:
        ds.close()


# --------------------------------------------------------------------------------------
# Regridding — building the shared routing cost field
# --------------------------------------------------------------------------------------


def regrid_to(grid: Grid, lats: np.ndarray, lons: np.ndarray, when: datetime | None = None) -> np.ndarray:
    """Nearest-neighbour resample ``grid`` onto ``(lats, lons)``.

    Fully vectorised via :func:`_nearest_indices` and fancy indexing — no Python loop —
    so it stays cheap when called once per source grid over a ~260 x 290 routing grid.
    """
    arr = grid.to_array(when)
    lat_idx = _nearest_indices(grid.lats, np.asarray(lats, dtype=float))
    lon_idx = _nearest_indices(grid.lons, np.asarray(lons, dtype=float))
    return arr[np.ix_(lat_idx, lon_idx)]


def stack_grids(
    grids: dict[str, Grid], lats: np.ndarray, lons: np.ndarray, when: datetime | None = None
) -> dict[str, np.ndarray]:
    """Regrid every grid in ``grids`` onto the common ``(lats, lons)`` target."""
    return {name: regrid_to(g, lats, lons, when=when) for name, g in grids.items()}


__all__ = ["Grid", "open_netcdf", "regrid_to", "stack_grids"]
