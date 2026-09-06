"""NOAA CoastWatch ERDDAP — ocean colour and the long SST record.

This adapter exists because two sources CLAUDE.md lists as working do not, in fact,
cover this coast. Both findings were re-probed live on 2026-09-06 before a line of this
module was written:

* ``incois.gov.in/thredds/.../osf/chl`` is a **Pacific Islands Countries** product. Its
  filenames say so (``VIIRS-SNPP-Roll-<start>-<end>-4KM-PICountries-CHL.nc``) and so does
  its grid: ``lat -25.979 .. 18.021``, ``lon 129.979 .. 215.021``. Palk Bay (78-80.6 E)
  is not in it, has never been in it, and the NCSS 400 that ``incois_thredds`` fast-fails
  on is that miss, not a transient. Chlorophyll — one of the two signals INCOIS's own PFZ
  method rests on — has therefore never once been available to this system.
* ``PFZ_Automation:pfzlines`` holds 65 features nationally and **every one of them** is
  ``Year=2021, Julian_day=248``. The official advisory line is frozen at 5 Sep 2021.

So chlorophyll has to come from somewhere, and the honest options over the Bay of Bengal
are NOAA's. Three keyless griddap datasets, each probed live over this region's bbox and
each returning real values here today:

============================================================ ============= ============== ===============
dataset                                                      variable      grid           verified
============================================================ ============= ============== ===============
``nesdisVHNnoaaSNPPnoaa20NRTchlaGapfilledDaily``             ``chlor_a``   2160 x 4320    to 2026-09-03
``erdMH1chla1day_R2022NRT``                                  ``chlorophyll`` 4320 x 8640  to 2026-09-04
``ncdcOisst21Agg``                                           ``sst``,``anom`` 720 x 1440  to 2026-08-21
============================================================ ============= ============== ===============

**Why the gap-filled product leads.** Optical chlorophyll over a monsoon coast is mostly
cloud. ``nesdisVHNnoaaSNPPnoaa20NRTchlaGapfilledDaily`` is DINEOF-interpolated across
S-NPP, NOAA-20 and Sentinel-3, so it returns a continuous field where a raw L3 composite
returns holes — 687 real cells in this bbox on the probe, against a raw grid that is
mostly NaN. It is coarser (1/12 deg) than MODIS (1/24 deg), which is the trade being
made: coverage over resolution, for a signal whose whole purpose is to show *where* the
front is, not to resolve it to the metre. MODIS is fetched as the finer second opinion
and the two are reported side by side, never averaged (CLAUDE.md, "Do not").

**Why OISST for the long record.** ``ncdcOisst21Agg`` returned 8615 daily values at
(9.45, 79.30) from 2003-01-01 with **zero** gaps, and carries its own ``anom`` anomaly
field against the 1971-2000 climatology — so the productivity diagnostic reports a
published anomaly rather than deriving one from a baseline it chose itself.

Three mechanical facts about ERDDAP that this module encodes, all of them learned the
expensive way in ``incois_erddap.py`` and re-confirmed here:

1. The constraint expression (``[(last)][(0.0)][(10.9):(8.0)][(78.0):(80.6)]``) is **one
   opaque expression**, not query parameters. Tomcat 400s on literal ``[ ] ( )`` in the
   request line, so the whole expression is percent-encoded and appended to the URL —
   never passed as ``params`` to :meth:`Source.get`.
2. **Latitude axis direction differs per dataset and getting it backwards returns a 404,
   not an empty result.** The two chlorophyll grids run north-to-south (constrain
   ``(10.9):(8.0)``); OISST runs south-to-north (constrain ``(8.0):(10.9)``). Read
   ``actual_range`` from ``.das`` and emit the constraint in the axis's own direction —
   do not hardcode a direction per dataset id, because the next dataset added here will
   have the other one.
3. Both chlorophyll datasets have a degenerate ``altitude`` axis and OISST a degenerate
   ``zlev``; MODIS has neither. The axis count is read from ``.dds``, not assumed.

Resolutions below are computed from each grid's own cell count over its own span, not
copied from a product title — the gap-filled dataset is titled "4km" in the CoastWatch
catalogue and is actually 1/12 deg.
"""

from __future__ import annotations

import csv
import io
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Sequence
from urllib.parse import quote

import numpy as np

from ..models import UTC, Observation, Provenance
from .base import FetchResult, Source, SourceError
from .incois_thredds import GridSlice

#: CoastWatch ERDDAP base. Keyless; no Referer requirement (unlike INCOIS/IMD), but the
#: shared client sends one anyway.
ERDDAP = "https://coastwatch.pfeg.noaa.gov/erddap"

#: Which chlorophyll product a caller wants. ``"gapfilled"`` is the default everywhere:
#: a continuous field is worth more to a front-detection method than a sharper one full
#: of cloud holes.
ChlProduct = Literal["gapfilled", "modis"]


@dataclass(frozen=True)
class DatasetSpec:
    """One griddap dataset, described by what was actually probed on it."""

    dataset_id: str
    #: Data variables to request, in the order they should appear in the CSV.
    variables: tuple[str, ...]
    #: Axis names in the dataset's own order, e.g. ``("time", "altitude", "latitude",
    #: "longitude")``. Degenerate axes are included — they still need a constraint.
    axes: tuple[str, ...]
    #: Constraint emitted for each degenerate (size-1) axis, by axis name.
    degenerate_axis_value: dict[str, str]
    #: Metres, computed from the grid's own cell count over its own span.
    spatial_resolution_m: float
    #: FORESHORE variable name each dataset variable maps to.
    variable_names: dict[str, str]
    #: Unit each mapped variable is emitted in.
    units: dict[str, str]
    #: Human sentence naming the product. Reaches the answer, so: no dataset ids.
    label: str


#: The three probed datasets. Every constant here was read off a live ``.das``/``.dds``
#: on 2026-09-06, never off a catalogue page.
DATASETS: dict[str, DatasetSpec] = {
    "gapfilled": DatasetSpec(
        dataset_id="nesdisVHNnoaaSNPPnoaa20NRTchlaGapfilledDaily",
        variables=("chlor_a",),
        axes=("time", "altitude", "latitude", "longitude"),
        degenerate_axis_value={"altitude": "(0.0)"},
        # 2160 latitude cells over 180 deg = 0.08333 deg; x 111_320 m/deg.
        spatial_resolution_m=9_277.0,
        variable_names={"chlor_a": "chlorophyll_a"},
        units={"chlor_a": "mg/m^3"},
        label=(
            "NOAA S-NPP / NOAA-20 / Sentinel-3 VIIRS gap-filled (DINEOF) chlorophyll, "
            "near real-time"
        ),
    ),
    "modis": DatasetSpec(
        dataset_id="erdMH1chla1day_R2022NRT",
        variables=("chlorophyll",),
        axes=("time", "latitude", "longitude"),
        degenerate_axis_value={},
        # 4320 latitude cells over 180 deg = 0.0416667 deg; x 111_320 m/deg.
        spatial_resolution_m=4_638.0,
        variable_names={"chlorophyll": "chlorophyll_a"},
        units={"chlorophyll": "mg/m^3"},
        label="NASA MODIS-Aqua L3 chlorophyll, near real-time",
    ),
    "sst": DatasetSpec(
        dataset_id="ncdcOisst21Agg",
        variables=("sst", "anom"),
        axes=("time", "zlev", "latitude", "longitude"),
        degenerate_axis_value={"zlev": "(0.0)"},
        # 720 latitude cells over 180 deg = 0.25 deg; x 111_320 m/deg.
        spatial_resolution_m=27_830.0,
        variable_names={"sst": "sea_surface_temperature", "anom": "sea_surface_temperature_anomaly"},
        units={"sst": "degC", "anom": "degC"},
        label="NOAA OISST v2.1 daily optimum-interpolation sea surface temperature",
    ),
}


# ----------------------------------------------------------------------------------
# ERDDAP wire-format helpers — copied from incois_erddap.py (see module docstring:
# private names are not shared across source modules, and both adapters independently
# target the same griddap protocol).
# ----------------------------------------------------------------------------------


def _parse_das(text: str) -> dict[str, dict[str, Any]]:
    """Parse a griddap ``.das`` response into ``{block_name: {attr_name: value}}``."""
    blocks: dict[str, str] = {}
    for m in re.finditer(r"^  (\w+) \{\n(.*?)\n  \}", text, re.M | re.S):
        blocks[m.group(1)] = m.group(2)

    def parse_block(body: str) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        spans: list[tuple[int, int]] = []
        quoted_re = re.compile(r'(\S+)\s+(\S+)\s+"((?:[^"\\]|\\.)*)"\s*;', re.S)
        for qm in quoted_re.finditer(body):
            _typ, name, val = qm.groups()
            attrs[name] = val.replace("\\n", "\n")
            spans.append((qm.start(), qm.end()))
        remainder_parts = []
        last = 0
        for s, e in spans:
            remainder_parts.append(body[last:s])
            last = e
        remainder_parts.append(body[last:])
        remainder = "".join(remainder_parts)
        num_re = re.compile(r'(\S+)\s+(\S+)\s+([^";\n]+);')
        for nm in num_re.finditer(remainder):
            _typ, name, val = nm.groups()
            val = val.strip()
            if "," in val:
                try:
                    attrs[name] = [float(x.strip()) for x in val.split(",")]
                    continue
                except ValueError:
                    pass
            try:
                attrs[name] = float(val)
            except ValueError:
                attrs[name] = val
        return attrs

    return {name: parse_block(body) for name, body in blocks.items()}


def _parse_dds_dims(text: str) -> dict[str, int]:
    """Pull ``NAME[NAME = N]`` dimension sizes out of a griddap ``.dds`` response."""
    sizes: dict[str, int] = {}
    for m in re.finditer(r"(\w+)\[\1\s*=\s*(\d+)\]", text):
        sizes[m.group(1)] = int(m.group(2))
    return sizes


def _iso_z(dt: datetime) -> str:
    dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_erddap_time(value: str | None) -> datetime | None:
    if not value:
        return None
    s = value.strip()
    if not s:
        return None
    s = s[:-1] if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _is_missing(s: str | None) -> bool:
    return s is None or s.strip() == "" or s.strip().lower() == "nan"


def _parse_csv_text(text: str) -> tuple[list[str], list[str], list[dict[str, str]]]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return [], [], []
    rows = list(csv.reader(io.StringIO("\n".join(lines))))
    cols = rows[0]
    units = rows[1] if len(rows) > 1 else ["" for _ in cols]
    data = [dict(zip(cols, r)) for r in rows[2:]]
    return cols, units, data


def _csv_url(dataset_id: str, variables: Sequence[str], dims_expr: str) -> str:
    """Percent-encode the *whole* constraint expression and append it to the URL —
    never pass brackets/parens as ``params`` (see module docstring, point 1)."""
    query = ",".join(f"{v}{dims_expr}" for v in variables)
    return f"{ERDDAP}/griddap/{dataset_id}.csv?{quote(query, safe='')}"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _actual_range(block: dict[str, Any]) -> tuple[float | None, float | None]:
    """``actual_range`` off one ``.das`` axis block, in the order ``.das`` gave it —
    that order *is* the axis's storage direction (see module docstring); this function
    never sorts the pair."""
    r = block.get("actual_range")
    if isinstance(r, list) and len(r) == 2:
        return float(r[0]), float(r[1])
    return None, None


# ----------------------------------------------------------------------------------
# Constraint-expression builders. Kept free-standing (not methods) so the acceptance
# test can drive them directly with literal ``DatasetSpec``/bbox values — no instance,
# no network.
# ----------------------------------------------------------------------------------


def _dims_expr(spec: DatasetSpec, *, time_expr: str, lat_expr: str, lon_expr: str) -> str:
    """Assemble the one opaque ``[...][...]...`` expression, axis by axis, in the
    dataset's own axis order. A degenerate axis (``altitude``/``zlev``) contributes its
    fixed :attr:`DatasetSpec.degenerate_axis_value` entry; MODIS has none and
    contributes nothing extra — the axis simply is not in :attr:`DatasetSpec.axes`."""
    parts: list[str] = []
    for axis in spec.axes:
        if axis == "time":
            parts.append(f"[{time_expr}]")
        elif axis == "latitude":
            parts.append(f"[{lat_expr}]")
        elif axis == "longitude":
            parts.append(f"[{lon_expr}]")
        else:
            parts.append(f"[{spec.degenerate_axis_value[axis]}]")
    return "".join(parts)


def _grid_dims_expr(
    spec: DatasetSpec, *, bbox: Sequence[float], lat_ascending: bool, time_expr: str
) -> str:
    """The bbox-range form used by :meth:`OceanColour.chlorophyll_slice`. ``bbox`` is
    ``(minlon, minlat, maxlon, maxlat)``; the latitude constraint is emitted in
    whichever direction ``lat_ascending`` (read from ``.das``, never hardcoded) says the
    axis is actually stored in — getting this backwards is the 404 the module docstring
    warns about, not an empty result."""
    minlon, minlat, maxlon, maxlat = bbox
    lat_expr = f"({minlat}):({maxlat})" if lat_ascending else f"({maxlat}):({minlat})"
    lon_expr = f"({minlon}):({maxlon})"
    return _dims_expr(spec, time_expr=time_expr, lat_expr=lat_expr, lon_expr=lon_expr)


def _point_dims_expr(spec: DatasetSpec, *, lat: float, lon: float, time_expr: str) -> str:
    """The point form used by the two ``*_series`` methods. A point constraint needs no
    direction — ERDDAP snaps to the nearest grid coordinate regardless of which way the
    axis is stored, and echoes the snapped value back in the response's own columns."""
    return _dims_expr(spec, time_expr=time_expr, lat_expr=f"({lat})", lon_expr=f"({lon})")


def _cache_key(
    dataset_id: str,
    operation: str,
    *,
    bbox: Sequence[float] | None = None,
    lat: float | None = None,
    lon: float | None = None,
    time_bound: str = "",
) -> str:
    """Deterministic cache/fixture identity — ``(dataset_id, operation, rounded bbox or
    lat/lon, iso time bounds)``, never a wall-clock instant computed inside this module
    (see D11 in ``docs/DECISIONS.md`` and ``test_incois_thredds_key.py``: a key that
    varies per microsecond means fixture mode never hits). ``time_bound`` is whatever
    the caller already resolved — ``"(last)"`` is a fixed string, not "now"."""
    parts = [dataset_id, operation]
    if bbox is not None:
        parts.append(",".join(f"{float(v):.4f}" for v in bbox))
    if lat is not None and lon is not None:
        parts.append(f"{float(lat):.4f}:{float(lon):.4f}")
    parts.append(time_bound)
    return "|".join(parts)


def _clamp_time_range(
    start: datetime,
    end: datetime,
    coverage_start: datetime | None,
    coverage_end: datetime | None,
) -> tuple[datetime, datetime, bool]:
    """Clamp ``[start, end]`` to ``[coverage_start, coverage_end]`` rather than letting a
    caller's out-of-range request 404. Returns ``(clamped_start, clamped_end, clamped)``
    — ``clamped`` is ``True`` the moment either bound moved, so
    :meth:`OceanColour.sst_series` can stamp every Observation's
    ``qualifiers["time_range_clamped"]`` honestly instead of hiding the fact that what
    was returned is not what was asked for (CLAUDE.md invariant 4: staleness/scope
    truncation is surfaced, never hidden)."""
    clamped = False
    new_start, new_end = start, end
    if coverage_start is not None and new_start < coverage_start:
        new_start = coverage_start
        clamped = True
    if coverage_end is not None and new_end > coverage_end:
        new_end = coverage_end
        clamped = True
    return new_start, new_end, clamped


class OceanColour(Source):
    """NOAA CoastWatch ERDDAP: live chlorophyll fields and the long daily SST record.

    Every method returns the system's existing types — :class:`GridSlice` for fields,
    :class:`Observation` for series — so nothing downstream needs to learn a new shape.
    A caller that had an INCOIS ``GridSlice`` can use one of these unchanged.
    """

    source_id = "noaa_coastwatch"
    source_name = "NOAA CoastWatch ERDDAP (VIIRS gap-filled chlorophyll, MODIS-Aqua chlorophyll, OISST v2.1)"
    authority = "NOAA"
    #: Overridden per dataset from :data:`DATASETS`; this is the coarsest, so a caller
    #: that forgets to look at the slice's own provenance under-claims rather than over-claims.
    spatial_resolution_m = 27_830.0
    #: Chlorophyll runs ~2-3 days behind and OISST ~2 weeks; neither is a nowcast and
    #: neither should ever be labelled "current".
    validity = timedelta(days=4)
    #: Fields change daily at most. Six hours keeps a demo warm without serving yesterday.
    cache_ttl_s = 21_600.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # In-memory only, life of this instance — mirrors IncoisThredds._catalog_cache.
        # Source.get() already layers its own snapshot/fixture cache underneath every
        # individual .das/.dds fetch; this just spares repeat metadata() calls within
        # one request from re-parsing text they already parsed.
        self._meta_cache: dict[str, dict[str, Any]] = {}

    # -- transport -----------------------------------------------------------------

    def _das_fetch(self, dataset_id: str) -> FetchResult:
        return self.get(f"{ERDDAP}/griddap/{dataset_id}.das", key=f"{dataset_id}:das")

    def _dds_fetch(self, dataset_id: str) -> FetchResult:
        return self.get(f"{ERDDAP}/griddap/{dataset_id}.dds", key=f"{dataset_id}:dds")

    # -- CSV -> array pivot ----------------------------------------------------------

    def _pivot_grid(
        self, rows: list[dict[str, str]], var_col: str
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Pivot ERDDAP grid-subset CSV rows (one row per lat/lon cell, in the axis's
        own storage order) into ``(lats, lons, array)`` with both axes **ascending**
        regardless of which direction the rows arrived in. ``NaN`` cells stay ``NaN`` —
        never zero-filled, never interpolated."""
        lats_seen: list[float] = []
        lat_index: dict[float, int] = {}
        lons_seen: list[float] = []
        lon_index: dict[float, int] = {}
        for row in rows:
            lat_v = float(row["latitude"])
            lon_v = float(row["longitude"])
            if lat_v not in lat_index:
                lat_index[lat_v] = len(lats_seen)
                lats_seen.append(lat_v)
            if lon_v not in lon_index:
                lon_index[lon_v] = len(lons_seen)
                lons_seen.append(lon_v)
        n_lat, n_lon = len(lats_seen), len(lons_seen)
        if n_lat == 0 or n_lon == 0 or n_lat * n_lon != len(rows):
            raise SourceError(
                self.source_id,
                f"grid-subset CSV did not pivot to a rectangle "
                f"({n_lat}x{n_lon} cells against {len(rows)} rows)",
            )
        arr = np.full((n_lat, n_lon), np.nan, dtype=float)
        for row in rows:
            lat_v = float(row["latitude"])
            lon_v = float(row["longitude"])
            val_s = row.get(var_col)
            if not _is_missing(val_s):
                arr[lat_index[lat_v], lon_index[lon_v]] = float(val_s)  # type: ignore[arg-type]
        lats = np.asarray(lats_seen, dtype=float)
        lons = np.asarray(lons_seen, dtype=float)
        if lats.size >= 2 and lats[0] > lats[-1]:
            lats = lats[::-1]
            arr = arr[::-1, :]
        if lons.size >= 2 and lons[0] > lons[-1]:
            lons = lons[::-1]
            arr = arr[:, ::-1]
        return lats, lons, arr

    # -- fields ------------------------------------------------------------------------

    def chlorophyll_slice(
        self,
        bbox: Sequence[float],
        *,
        at: datetime | None = None,
        product: ChlProduct = "gapfilled",
    ) -> GridSlice:
        """A 2-D chlorophyll field over ``bbox``, as the same :class:`GridSlice` the
        INCOIS THREDDS adapter returns.

        ``bbox`` is ``(minlon, minlat, maxlon, maxlat)`` — the region-config order, not
        ERDDAP's. ``at=None`` takes the dataset's latest step (``(last)``).

        The returned slice carries ``variables={"chlorophyll_a": <2-D array>}`` with
        ``lats`` ascending and ``lons`` ascending regardless of the dataset's own axis
        direction, ``file_date`` set to the composite's own date, ``history`` naming the
        product, ``local_path=None`` and a :class:`~foreshore.models.Provenance` whose
        ``spatial_resolution_m`` is this dataset's, whose ``issued_at`` is the step's own
        time, and whose ``is_derived`` is ``False`` — this is a published field, not a
        FORESHORE derivation.

        Fill values arrive as the literal ``NaN`` in CSV and stay ``np.nan`` in the
        array. This method never substitutes a value for a hole.

        Raises :class:`~foreshore.sources.base.SourceError` on transport failure or on a
        bbox that misses the grid. It does **not** swallow those into an empty slice:
        callers distinguish "no chlorophyll here" from "chlorophyll fetch failed", and
        merging the two is how the INCOIS Pacific-grid miss stayed invisible for weeks.
        """
        if product not in ("gapfilled", "modis"):
            raise SourceError(
                self.source_id,
                f"chlorophyll_slice: unsupported product {product!r}; choose 'gapfilled' or 'modis'",
            )
        spec = DATASETS[product]
        meta = self.metadata(product)
        lat_ascending = bool(meta["lat_ascending"])
        time_expr = "(last)" if at is None else f"({_iso_z(at)})"
        dims_expr = _grid_dims_expr(spec, bbox=bbox, lat_ascending=lat_ascending, time_expr=time_expr)
        url = _csv_url(spec.dataset_id, spec.variables, dims_expr)
        key = _cache_key(spec.dataset_id, "chlorophyll_slice", bbox=bbox, time_bound=time_expr)
        raw = self.get(url, key=key)
        _cols, _units, rows = _parse_csv_text(raw.text)
        if not rows:
            raise SourceError(
                self.source_id,
                "chlorophyll_slice: no grid cells returned for the requested bbox",
            )
        var_col = spec.variables[0]
        lats, lons, arr = self._pivot_grid(rows, var_col)
        valid_time = _parse_erddap_time(rows[0].get("time")) or raw.acquired_at
        prov = self.provenance(
            raw, issued_at=valid_time, valid_from=valid_time,
            spatial_resolution_m=spec.spatial_resolution_m, notes=spec.label,
        )
        canonical = spec.variable_names[var_col]
        return GridSlice(
            product=product,
            variables={canonical: arr},
            lats=lats,
            lons=lons,
            valid_time=valid_time,
            file_date=valid_time.date(),
            local_path=None,
            history=spec.label,
            provenance=prov,
        )

    # -- series ------------------------------------------------------------------------

    def chlorophyll_series(
        self,
        lat: float,
        lon: float,
        *,
        start: datetime,
        end: datetime,
        product: ChlProduct = "gapfilled",
    ) -> list[Observation]:
        """Chlorophyll at one point across a time span, one Observation per step.

        Each Observation is ``variable="chlorophyll_a"``, ``unit="mg/m^3"``, positioned at
        the **snapped grid cell** ERDDAP echoes back — not at the requested point — with
        ``requested_lat``/``requested_lon`` and ``grid_lat``/``grid_lon`` in
        ``qualifiers`` so the difference is visible rather than quietly absorbed.
        ``NaN`` steps are skipped, never zero-filled.
        """
        if product not in ("gapfilled", "modis"):
            raise SourceError(
                self.source_id,
                f"chlorophyll_series: unsupported product {product!r}; choose 'gapfilled' or 'modis'",
            )
        spec = DATASETS[product]
        start = _aware(start)
        end = _aware(end)
        time_expr = f"({_iso_z(start)}):({_iso_z(end)})"
        dims_expr = _point_dims_expr(spec, lat=lat, lon=lon, time_expr=time_expr)
        url = _csv_url(spec.dataset_id, spec.variables, dims_expr)
        key = _cache_key(
            spec.dataset_id, "chlorophyll_series", lat=lat, lon=lon,
            time_bound=f"{_iso_z(start)}:{_iso_z(end)}",
        )
        raw = self.get(url, key=key)
        _cols, _units, rows = _parse_csv_text(raw.text)
        var_col = spec.variables[0]
        canonical = spec.variable_names[var_col]
        unit = spec.units[var_col]
        out: list[Observation] = []
        for row in rows:
            val_s = row.get(var_col)
            if _is_missing(val_s):
                continue
            valid_time = _parse_erddap_time(row.get("time")) or raw.acquired_at
            grid_lat = float(row["latitude"]) if not _is_missing(row.get("latitude")) else lat
            grid_lon = float(row["longitude"]) if not _is_missing(row.get("longitude")) else lon
            prov = self.provenance(
                raw, issued_at=valid_time, valid_from=valid_time,
                spatial_resolution_m=spec.spatial_resolution_m, notes=spec.label,
            )
            out.append(self.observe(
                canonical, float(val_s), unit, grid_lat, grid_lon, valid_time, prov,  # type: ignore[arg-type]
                requested_lat=lat, requested_lon=lon, grid_lat=grid_lat, grid_lon=grid_lon,
            ))
        return out

    def sst_series(
        self, lat: float, lon: float, *, start: datetime, end: datetime
    ) -> list[Observation]:
        """Daily SST **and** its published anomaly at one point across a time span.

        Emits two Observations per step: ``sea_surface_temperature`` and
        ``sea_surface_temperature_anomaly``, both ``degC``. The anomaly is OISST's own,
        against its own climatology — FORESHORE does not compute an anomaly, so it cannot
        be accused of choosing a flattering baseline.

        ``ncdcOisst21Agg`` spans 1981-09-01 to roughly two weeks behind today; a ``start``
        before the record or an ``end`` past it is clamped to the dataset's own coverage
        rather than 404-ing, and the clamp is recorded in every Observation's
        ``qualifiers["time_range_clamped"]``.
        """
        spec = DATASETS["sst"]
        meta = self.metadata("sst")
        coverage_start = _parse_erddap_time(meta.get("time_coverage_start"))
        coverage_end = _parse_erddap_time(meta.get("time_coverage_end"))
        start = _aware(start)
        end = _aware(end)
        clamped_start, clamped_end, clamped = _clamp_time_range(
            start, end, coverage_start, coverage_end
        )
        if clamped_end <= clamped_start:
            raise SourceError(
                self.source_id,
                "sst_series: requested time range falls entirely outside the OISST record",
            )
        time_expr = f"({_iso_z(clamped_start)}):({_iso_z(clamped_end)})"
        dims_expr = _point_dims_expr(spec, lat=lat, lon=lon, time_expr=time_expr)
        url = _csv_url(spec.dataset_id, spec.variables, dims_expr)
        key = _cache_key(
            spec.dataset_id, "sst_series", lat=lat, lon=lon,
            time_bound=f"{_iso_z(clamped_start)}:{_iso_z(clamped_end)}",
        )
        raw = self.get(url, key=key)
        _cols, _units, rows = _parse_csv_text(raw.text)
        out: list[Observation] = []
        for row in rows:
            valid_time = _parse_erddap_time(row.get("time")) or raw.acquired_at
            grid_lat = float(row["latitude"]) if not _is_missing(row.get("latitude")) else lat
            grid_lon = float(row["longitude"]) if not _is_missing(row.get("longitude")) else lon
            prov = self.provenance(
                raw, issued_at=valid_time, valid_from=valid_time,
                spatial_resolution_m=spec.spatial_resolution_m, notes=spec.label,
            )
            for raw_var in spec.variables:
                val_s = row.get(raw_var)
                if _is_missing(val_s):
                    continue
                canonical = spec.variable_names[raw_var]
                unit = spec.units[raw_var]
                out.append(self.observe(
                    canonical, float(val_s), unit, grid_lat, grid_lon, valid_time, prov,  # type: ignore[arg-type]
                    requested_lat=lat, requested_lon=lon, grid_lat=grid_lat, grid_lon=grid_lon,
                    time_range_clamped=clamped,
                ))
        return out

    # -- generic Source contract --------------------------------------------------------

    def metadata(self, product: str) -> dict[str, Any]:
        """``.das``/``.dds`` for one entry of :data:`DATASETS`, parsed.

        Must return at least ``time_coverage_start``, ``time_coverage_end``,
        ``lat_range``, ``lon_range``, ``axis_sizes`` and ``lat_ascending`` — that last is
        what keeps constraint direction (see module docstring, point 2) read from the
        data rather than hardcoded.
        """
        if product not in DATASETS:
            raise SourceError(
                self.source_id, f"metadata: unknown product {product!r}; choose from {sorted(DATASETS)}"
            )
        if product in self._meta_cache:
            return self._meta_cache[product]
        spec = DATASETS[product]
        das_raw = self._das_fetch(spec.dataset_id)
        dds_raw = self._dds_fetch(spec.dataset_id)
        das = _parse_das(das_raw.text)
        axis_sizes = _parse_dds_dims(dds_raw.text)
        global_attrs = das.get("NC_GLOBAL", {})

        lat_lo, lat_hi = _actual_range(das.get("latitude", {}))
        lon_lo, lon_hi = _actual_range(das.get("longitude", {}))
        # actual_range preserves storage direction (module docstring) — the axis is
        # ascending unless the two numbers arrived high-to-low. An axis this module has
        # never probed a range for degrades to "ascending" (the more common case)
        # rather than raising, since a metadata call must still be able to answer.
        lat_ascending = lat_lo is None or lat_hi is None or lat_lo <= lat_hi

        meta = {
            "dataset_id": spec.dataset_id,
            "axis_sizes": {axis: axis_sizes.get(axis) for axis in spec.axes},
            "lat_range": (lat_lo, lat_hi),
            "lon_range": (lon_lo, lon_hi),
            "lat_ascending": lat_ascending,
            "time_coverage_start": global_attrs.get("time_coverage_start"),
            "time_coverage_end": global_attrs.get("time_coverage_end"),
        }
        self._meta_cache[product] = meta
        return meta

    def parse(self, raw: FetchResult, **kw: Any) -> list[Observation]:
        """A ``.das``/``.dds`` fetch carries no values and parses to ``[]`` — valid, not
        an error, exactly as in ``incois_erddap.py``."""
        return []

    def fetch(self, **kwargs: Any) -> FetchResult:
        product = kwargs.get("product", "gapfilled")
        spec = DATASETS.get(product, DATASETS["gapfilled"])
        return self._das_fetch(spec.dataset_id)

    def health(self) -> dict[str, Any]:
        """One row per dataset, so ``scripts/healthcheck.py`` shows which of the three is
        lagging rather than a single OK that hides two dead products."""
        t0 = time.perf_counter()
        rows: dict[str, dict[str, Any]] = {key: self._dataset_health(key) for key in DATASETS}
        ok = any(r["ok"] for r in rows.values())
        count = sum(r.get("count") or 0 for r in rows.values())
        issued_candidates = [r["issued_at"] for r in rows.values() if r.get("issued_at")]
        errors = [f"{key}: {r['error']}" for key, r in rows.items() if not r["ok"] and r.get("error")]
        return {
            "source_id": self.source_id,
            "ok": ok,
            "count": count,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "issued_at": max(issued_candidates) if issued_candidates else None,
            "error": "; ".join(errors) or None,
            "datasets": [rows[key] for key in DATASETS],
        }

    def _dataset_health(self, key: str) -> dict[str, Any]:
        """One :data:`DATASETS` entry's row, shaped like ``IncoisArgo.health()``
        (``source_id``, ``ok``, ``count``, ``latency_ms``, ``issued_at``, ``error``) plus
        the dataset's own ``time_coverage_start``/``time_coverage_end``."""
        t0 = time.perf_counter()
        try:
            meta = self.metadata(key)
            issued = _parse_erddap_time(meta.get("time_coverage_end"))
            return {
                "source_id": f"{self.source_id}:{key}",
                "ok": True,
                "count": len(DATASETS[key].variables),
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "issued_at": issued.isoformat() if issued else meta.get("time_coverage_end"),
                "error": None,
                "time_coverage_start": meta.get("time_coverage_start"),
                "time_coverage_end": meta.get("time_coverage_end"),
            }
        except Exception as exc:  # noqa: BLE001 - one dead dataset must not fail the other two
            return {
                "source_id": f"{self.source_id}:{key}",
                "ok": False,
                "count": 0,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "issued_at": None,
                "error": f"{type(exc).__name__}: {exc}",
                "time_coverage_start": None,
                "time_coverage_end": None,
            }


__all__ = ["OceanColour", "DatasetSpec", "DATASETS", "ChlProduct", "ERDDAP"]
