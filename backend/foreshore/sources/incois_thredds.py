"""INCOIS THREDDS / Ocean State Forecast (OSF) adapter — the authoritative wave model.

Probed live against the real server on 2026-08-30/31 (system clock 2026-08-31; the
newest catalogue entry on every product was dated 2026-08-29, confirming the documented
~2-day OSF lag) before a single variable name was hardcoded here. Six products live under
``https://incois.gov.in/thredds/catalog/osf/<product>/catalog.xml``: ``wave``, ``mwh``,
``currents``, ``winds``, ``sst``, ``chl``. Findings, so nobody has to re-probe:

**Transport.** Catalogue listings and NCSS ``dataset.xml`` probes are plain XML/text and
go through :meth:`Source.get` like every other adapter (browser UA + Referer, retry with
backoff, snapshot cache, fixture replay — all inherited for free). The actual **grid
data** returned by the NetCDF Subset Service (NCSS) is binary NetCDF3, and
``Source.get`` cannot carry it: it unconditionally branches on ``resp.json()`` vs
``resp.text`` and would corrupt binary bytes and mis-serialise them into a fixture/cache
JSON record. This module therefore fetches grid bytes with its own small helper
(:func:`IncoisThredds._fetch_grid_bytes`) that reuses the *same* shared ``httpx`` client,
the *same* browser ``User-Agent``/``Referer`` headers, and the *same* retry-with-backoff
and fixture/cache-or-die discipline as ``Source.get`` — via ``base.client()``,
``base.default_referer()``, ``config.is_fixture()`` and ``store.cache.cache_binary`` /
``binary_path`` — it just cannot literally *call* ``self.get`` for this one payload
shape. ``FORESHORE_MODE=fixture`` opens no socket here either: it goes straight to
``binary_path(...)`` and raises a clear error if nothing was frozen.

**NCSS accept format — netcdf3, not netcdf4.** Every product's ``dataset.xml``
``<AcceptList>`` advertises exactly one option: ``netcdf3``. Requesting ``netcdf4``
either 400s or silently ignores the param depending on the product; ``accept=netcdf3``
is what this module sends everywhere.

**Real file prefixes, per product** (``catalog.xml`` under ``osf/<product>/``):

* ``wave``     → ``WAVES_coast_YYYYMMDD.nc`` (plus ``WAVES_io_*`` / ``WAVES_nio_*``
  siblings, not used — ``coast`` is the 0.1° assimilated coastal nest the whole system
  calls authoritative).
* ``mwh``      → ``MWH_coast_YYYYMMDD.nc`` (plus ``io``/``nio`` siblings, not used).
* ``currents`` → ``CURRENTS_IO_YYYYMMDD.nc`` / ``CURRENTS_NIO_YYYYMMDD.nc`` — **no
  ``coast`` nest exists for currents.** Both variants publish on the identical
  1080×720, 0.0833° grid (30°E–120°E, -30°N–30°N) and, probed side by side over this
  region's bbox on 2026-08-29's run, returned byte-identical ``CURRENT``/``U``/``V``
  values — they are two labels (``IO_HOOFS`` vs ``NIO_HOOFS`` per the ``history``
  attribute) over what is, in this bbox, the same field. ``NIO`` (North Indian Ocean) is
  used here as the domain-appropriate name for a Tamil Nadu region.
* ``winds``    → ``WINDS_YYYYMMDD.nc`` — single global-ish grid, no regional variants.
* ``sst``      → ``SST_IO_YYYYMMDD.nc`` / ``SST_NIO_YYYYMMDD.nc`` — same IO/NIO split and
  same identical-grid finding as currents; ``NIO`` used for the same reason.
* ``chl``      → **not** ``<PREFIX>_YYYYMMDD.nc``. Filenames are
  ``VIIRS-SNPP-Roll-<start8>-<end8>-4KM-PICountries-CHL.nc`` (e.g.
  ``VIIRS-SNPP-Roll-20260827-20260829-4KM-PICountries-CHL.nc``), a 3-day rolling
  composite named by its start/end dates. This module parses those two embedded dates
  directly out of the catalogue rather than guessing a filename from a date. **The grid
  itself, probed live, spans roughly 130°E–215°E** (Pacific-centred — "PICountries" is
  literally Pacific Islands Countries) and does **not** intersect this system's
  65–95°E Indian-Ocean bbox on the two dates probed; requesting it over the Palk
  Bay/Gulf of Mannar bbox 400s. This is reported to the caller as a normal
  :class:`~foreshore.sources.base.SourceError` (out-of-coverage, not a crash), and
  ``health()`` degrades ``chl`` individually rather than failing the adapter — see
  invariant "no unsourced numbers": a product whose published grid does not cover this
  region does not get numbers invented for it.

**Real variable names, per product ``dataset.xml``** (`<grid name=...>`):

* ``wave``: ``SWH, SWELL, WP, SWP, SWHX, SWHY, SWELLX, SWELLY`` — component vectors
  (``*X``/``*Y``) not used, only the scalar fields. ``history`` (per-variable attribute,
  not the dataset-global one — the dataset-global ``history`` is a PyFerret/NcML
  translation stamp, not model provenance) reads
  ``".../Mww3/ECMWF/With_Data_assimilation/temp_coast_YYYYMMDD.nc"``.
* ``mwh``: ``MAXW``. Per-variable ``history`` reads
  ``".../Mww3/ECMWF/Without_Data_assimilation/MaxWH/coast_max_YYYYMMDD.2026.nc"`` —
  note this is the *without*-assimilation run, unlike ``wave``. **Both catalogue dates
  probed (2026-08-28 and 2026-08-29) returned an all-NaN ``MAXW`` field over the entire
  published domain**, not just this region — a genuine upstream outage on the probe
  dates, exactly the "INCOIS occasionally 503s / degrades" case the brief warns about.
  The contract (``MWH_coast``) is implemented as specified regardless; ``point``/
  ``series`` correctly return zero observations rather than fabricating a value, and
  ``health()`` reports ``mwh`` as degraded without failing the other five products.
* ``currents``: ``CURRENT`` (speed, m/s, scalar), ``U`` (``eastward_current``), ``V``
  (``northward_current``). No direction field is published — ``current_direction`` is
  derived here from ``U``/``V`` via ``atan2`` (oceanographic "flowing toward" bearing).
* ``winds``: ``WSM`` (speed, m/s), ``WSXM`` (``eastward_wind``), ``WSYM``
  (``northward_wind``). ``wind_direction`` is derived from ``WSXM``/``WSYM`` via the
  meteorological "blowing from" convention (bearing + 180°).
* ``sst``: ``SST`` (°C).
* ``chl``: ``chlor_a`` (``mg m^-3``, OCI algorithm).

**Axis names are not uniform across products** (``LAT``/``LON`` for wave/currents/sst,
``LATITUDE``/``LONGITUDE`` for mwh, ``AX005``/``AX004`` for winds, ``lat``/``lon`` for
chl) but every axis in every product carries a CF ``standard_name``
(``latitude``/``longitude``/``time``/``depth``) and, where relevant, a CF ``axis``
attribute (``X``/``Y``/``T``/``Z``). This module resolves axes generically from those
attributes (:func:`_axis_dims`) instead of hardcoding per-product dimension names.
``xarray``'s default CF decoding correctly turns each product's very different time
units (``"hours since 0001-01-01"``, ``"days since 1990-01-01"``,
``"hours since 2026-08-27 01:30"``, ``"hours since 1901-01-15"``) into UTC-naive
``datetime64`` — verified by direct probe, not assumed — which this module then attaches
``UTC`` tzinfo to.

**Fill values**: every product declares ``_FillValue``/``missing_value`` (``-1e34`` for
the OSF products, ``0.0`` for winds, ``-999.9`` for SST, ``-32767`` for chl); ``xarray``'s
default ``mask_and_scale`` already turns these into ``NaN`` on load, and this module
never emits a NaN cell — :meth:`GridSlice.value_at` turns it into ``None`` and the
observation is omitted.

**Grid resolutions used for ``Provenance.spatial_resolution_m``**: wave/mwh 0.1°≈11 km;
currents/sst 0.0833°≈9.26 km; winds 0.1°≈11 km; chl 4 km (vendor-documented, matches the
CLAUDE.md operational note — this module never actually returns chl data for this region
on the dates probed, but the constant is recorded for when the published grid does
cover it).
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd
import xarray as xr

from ..config import is_fixture
from ..models import UTC, Observation, Provenance, haversine_m, utcnow
from .base import Source, SourceError, client, default_referer
from ..store.cache import binary_path, cache_binary

log = logging.getLogger("foreshore.sources.incois_thredds")

THREDDS = "https://incois.gov.in/thredds"

#: product -> (file prefix used to recognise a catalogue entry, default raw vars to
#: fetch when the caller asks for "everything", nominal spatial resolution in metres).
PRODUCTS: dict[str, tuple[str, list[str], float]] = {
    "wave": ("WAVES_coast", ["SWH", "SWELL", "WP", "SWP"], 11000.0),
    "mwh": ("MWH_coast", ["MAXW"], 11000.0),
    "currents": ("CURRENTS_NIO", ["CURRENT", "U", "V"], 9260.0),
    "winds": ("WINDS", ["WSM", "WSXM", "WSYM"], 11000.0),
    "sst": ("SST_NIO", ["SST"], 9260.0),
    "chl": ("VIIRS-SNPP-Roll", ["chlor_a"], 4000.0),
}

#: canonical variable name -> unit emitted. Used exactly by the rest of the system.
UNITS: dict[str, str] = {
    "significant_wave_height": "m",
    "swell_wave_height": "m",
    "wave_period": "s",
    "swell_wave_period": "s",
    "max_wave_height": "m",
    "current_speed": "m/s",
    "current_direction": "deg",
    "wind_speed": "m/s",
    "wind_direction": "deg",
    "sea_surface_temperature": "degC",
    "chlorophyll_a": "mg/m^3",
}

#: canonical var -> (owning product, raw NetCDF var(s) needed to build it). Direction
#: vars need two component fields; every other canonical var is a 1:1 raw passthrough.
VAR_DEPENDS: dict[str, tuple[str, list[str]]] = {
    "significant_wave_height": ("wave", ["SWH"]),
    "swell_wave_height": ("wave", ["SWELL"]),
    "wave_period": ("wave", ["WP"]),
    "swell_wave_period": ("wave", ["SWP"]),
    "max_wave_height": ("mwh", ["MAXW"]),
    "current_speed": ("currents", ["CURRENT"]),
    "current_direction": ("currents", ["U", "V"]),
    "wind_speed": ("winds", ["WSM"]),
    "wind_direction": ("winds", ["WSXM", "WSYM"]),
    "sea_surface_temperature": ("sst", ["SST"]),
    "chlorophyll_a": ("chl", ["chlor_a"]),
}

#: canonical vars owned by each product, in the order health()/point() present them.
PRODUCT_CANONICAL_VARS: dict[str, list[str]] = {
    "wave": ["significant_wave_height", "swell_wave_height", "wave_period", "swell_wave_period"],
    "mwh": ["max_wave_height"],
    "currents": ["current_speed", "current_direction"],
    "winds": ["wind_speed", "wind_direction"],
    "sst": ["sea_surface_temperature"],
    "chl": ["chlorophyll_a"],
}

#: 1:1 canonical -> raw var name (direction vars are handled separately — derived).
_CANON_TO_RAW_SINGLE: dict[str, str] = {
    "significant_wave_height": "SWH",
    "swell_wave_height": "SWELL",
    "wave_period": "WP",
    "swell_wave_period": "SWP",
    "max_wave_height": "MAXW",
    "current_speed": "CURRENT",
    "wind_speed": "WSM",
    "sea_surface_temperature": "SST",
    "chlorophyll_a": "chlor_a",
}

#: qualifiers["source_variable"] label per canonical var — literal raw name for direct
#: passthroughs, an honest "derived from" label for the two computed directions.
_SOURCE_VAR_LABEL: dict[str, str] = {
    **_CANON_TO_RAW_SINGLE,
    "current_direction": "U,V (derived bearing, atan2)",
    "wind_direction": "WSXM,WSYM (derived meteorological from-direction)",
}

#: 3-hourly OSF products per CLAUDE.md; winds is 6-hourly (probed: TAX resolution=6.0);
#: chl is a rolling composite, not a regular time series (no time axis at all).
_TEMPORAL_RES_S: dict[str, float | None] = {
    "wave": 10800.0,
    "mwh": 10800.0,
    "currents": 10800.0,
    "sst": 10800.0,
    "winds": 21600.0,
    "chl": None,
}

#: how far past issued_at (00:00 UTC of the file date) a record stays valid. +7 days for
#: the five forward-looking OSF forecast products per the plan's contract; chl is an
#: observational rolling composite, not a forecast, so it gets its own documented
#: 3-day span (matching "3-day rolling composite") instead of a fabricated 7-day one.
_VALID_SPAN: dict[str, timedelta] = {
    "wave": timedelta(days=7),
    "mwh": timedelta(days=7),
    "currents": timedelta(days=7),
    "sst": timedelta(days=7),
    "winds": timedelta(days=7),
    "chl": timedelta(days=3),
}

_CATALOG_NS = "{http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0}"
_STANDARD_PATTERN = re.compile(r"^(\d{8})\.nc$")
_CHL_PATTERN = re.compile(r"^VIIRS-SNPP-Roll-(\d{8})-(\d{8})-4KM-PICountries-CHL\.nc$")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _dt64_to_aware(v: Any) -> datetime:
    ts = pd.Timestamp(v)
    py = ts.to_pydatetime()
    return py if py.tzinfo else py.replace(tzinfo=UTC)


def _iso_z(dt: datetime) -> str:
    return _aware(dt).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bearing_to(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Compass bearing (0=N, clockwise) the (u, v) vector points *toward*."""
    return (np.degrees(np.arctan2(u, v)) + 360.0) % 360.0


def _met_from(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Meteorological direction the (u, v) wind is blowing *from*."""
    return (_bearing_to(u, v) + 180.0) % 360.0


def _axis_dims(ds: xr.Dataset) -> tuple[str, str, str | None, str | None]:
    """Resolve (lon_dim, lat_dim, time_dim, depth_dim) from CF ``axis``/``standard_name``
    attributes rather than hardcoded per-product dimension names — see module docstring:
    every product uses different axis names but all carry CF metadata."""
    xdim = ydim = tdim = zdim = None
    for name, coord in ds.coords.items():
        axis = str(coord.attrs.get("axis", "")).upper()
        std = str(coord.attrs.get("standard_name", "")).lower()
        if axis == "X" or std == "longitude":
            xdim = name
        elif axis == "Y" or std == "latitude":
            ydim = name
        elif axis == "T" or std == "time":
            tdim = name
        elif axis == "Z" or std == "depth":
            zdim = name
    if xdim is None or ydim is None:
        raise SourceError("incois_osf", f"could not resolve lat/lon axes; coords={list(ds.coords)}")
    return str(xdim), str(ydim), (str(tdim) if tdim is not None else None), (str(zdim) if zdim is not None else None)


def _select_2d(
    ds: xr.Dataset, raw_name: str, ti: int, xdim: str, ydim: str, tdim: str | None, zdim: str | None
) -> np.ndarray:
    da = ds[raw_name]
    sel: dict[str, int] = {}
    if tdim is not None and tdim in da.dims:
        sel[tdim] = ti
    if zdim is not None and zdim in da.dims:
        sel[zdim] = 0
    da2 = da.isel(**sel) if sel else da
    da2 = da2.transpose(ydim, xdim)
    return np.asarray(da2.values, dtype=float)


@dataclass
class _BlobFetch:
    path: Path
    acquired_at: datetime
    from_fixture: bool
    from_cache: bool


@dataclass(frozen=True)
class GridSlice:
    """One product, one timestep, one bbox — the unit :meth:`IncoisThredds.slice` and
    :meth:`IncoisThredds.series` build and hand to callers."""

    product: str
    variables: dict[str, np.ndarray]
    lats: np.ndarray
    lons: np.ndarray
    valid_time: datetime
    file_date: date
    local_path: Path | None
    history: str | None
    provenance: Provenance

    def value_at(self, var: str, lat: float, lon: float) -> tuple[float | None, float]:
        """Nearest-cell value and the distance in metres to that cell centre. A missing
        variable, or a cell holding the product's fill value (already NaN by the time it
        reaches here — see module docstring), both yield ``(None, distance_m)``: the
        distance is always computable from the grid alone, but a value is never
        fabricated for a hole in the data."""
        iy = int(np.abs(self.lats - lat).argmin())
        ix = int(np.abs(self.lons - lon).argmin())
        dist = haversine_m(lat, lon, float(self.lats[iy]), float(self.lons[ix]))
        arr = self.variables.get(var)
        if arr is None:
            return None, dist
        val = arr[iy, ix]
        if val is None or (isinstance(val, float) and math.isnan(val)) or bool(np.isnan(val)):
            return None, dist
        return float(val), dist


class IncoisThredds(Source):
    """INCOIS Ocean State Forecast (THREDDS/NCSS) — the authoritative wave model, plus
    currents/winds/SST/chlorophyll from the same OSF pipeline. See module docstring for
    every variable name, file prefix and transport detail discovered live."""

    source_id = "incois_osf"
    source_name = "INCOIS Ocean State Forecast (MWW3/ECMWF, with data assimilation)"
    authority = "INCOIS"
    validity = timedelta(days=1)
    cache_ttl_s = 3600.0
    spatial_resolution_m = 11000.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._catalog_cache: dict[str, dict[date, str]] = {}

    # -- catalogue -----------------------------------------------------------------

    def _catalog_map(self, product: str) -> dict[date, str]:
        """{file_date -> urlPath} for one product, discovered from catalog.xml. Cached
        in-memory for the life of this instance; ``self.get`` layers its own snapshot
        cache / fixture replay underneath (:attr:`cache_ttl_s`)."""
        if product in self._catalog_cache:
            return self._catalog_cache[product]
        if product not in PRODUCTS:
            raise SourceError(self.source_id, f"unknown product {product!r}; choose from {sorted(PRODUCTS)}")
        prefix, _default_vars, _res = PRODUCTS[product]
        url = f"{THREDDS}/catalog/osf/{product}/catalog.xml"
        raw = self.get(url, key=f"catalog-{product}")
        try:
            root = ET.fromstring(raw.text)
        except ET.ParseError as exc:
            raise SourceError(self.source_id, f"catalog.xml for {product!r} did not parse: {exc}") from exc
        mapping: dict[date, str] = {}
        for ds_el in root.iter(f"{_CATALOG_NS}dataset"):
            name = ds_el.attrib.get("name", "")
            url_path = ds_el.attrib.get("urlPath")
            if not url_path:
                continue
            d = self._parse_file_date(product, prefix, name)
            if d is not None:
                mapping[d] = url_path
        if not mapping:
            raise SourceError(self.source_id, f"no {product!r} datasets matched prefix {prefix!r} in catalog.xml")
        self._catalog_cache[product] = mapping
        return mapping

    @staticmethod
    def _parse_file_date(product: str, prefix: str, filename: str) -> date | None:
        if product == "chl":
            m = _CHL_PATTERN.match(filename)
            if not m:
                return None
            end8 = m.group(2)
            return datetime.strptime(end8, "%Y%m%d").date()
        if not filename.startswith(prefix + "_"):
            return None
        rest = filename[len(prefix) + 1 :]
        m = _STANDARD_PATTERN.match(rest)
        if not m:
            return None
        return datetime.strptime(m.group(1), "%Y%m%d").date()

    def catalog_dates(self, product: str) -> list[date]:
        return sorted(self._catalog_map(product).keys())

    def latest_date(self, product: str) -> date:
        dates = self.catalog_dates(product)
        if not dates:
            raise SourceError(self.source_id, f"no catalog entries discovered for product {product!r}")
        return dates[-1]

    def _resolve_file_date(self, product: str, at: datetime | None) -> date:
        dates = self.catalog_dates(product)
        if at is None:
            return dates[-1]
        target = _aware(at)
        candidates = [d for d in dates if datetime(d.year, d.month, d.day, tzinfo=UTC) <= target]
        return candidates[-1] if candidates else dates[-1]

    # -- variable resolution ---------------------------------------------------------

    def _raw_vars_for(self, product: str, variables: Sequence[str] | None) -> tuple[list[str], list[str]]:
        """(requested canonical vars, raw NetCDF vars needed to build them)."""
        canon_list = PRODUCT_CANONICAL_VARS[product]
        if variables:
            requested = [v for v in variables if v in canon_list]
            if not requested:
                log.warning(
                    "%s: none of requested variables %r belong to product %r (owns %r); using defaults",
                    self.source_id, list(variables), product, canon_list,
                )
                requested = list(canon_list)
        else:
            requested = list(canon_list)
        raw: list[str] = []
        for c in requested:
            _owner, needs = VAR_DEPENDS[c]
            for r in needs:
                if r not in raw:
                    raw.append(r)
        return requested, raw

    # -- binary transport (see module docstring: cannot route through self.get) ------

    def _binary_key(
        self, urlPath: str, raw_vars: Sequence[str], bbox: tuple[float, float, float, float],
    ) -> str:
        """Cache/fixture identity for a grid fetch — deliberately **not** a function of
        any request-time time-window. ``urlPath`` already names the specific day's file
        (the thing that actually determines what data exists); a ``time_start``/
        ``time_end`` NCSS subset window is a live-request bandwidth optimisation only
        (see ``_fetch_grid_bytes``), not a second axis of identity. It used to be part of
        this hash, computed from ``at ± 6h`` where ``at`` is frequently "now" resolved
        fresh per request — so two logically identical "what's the sea state right now"
        calls, microseconds apart, hashed to two different keys, and in
        ``FORESHORE_MODE=fixture`` only one of them could ever have been frozen. A demo
        asking about "now" would then non-deterministically, and after enough real time
        passed essentially *permanently*, report the INCOIS OSF wave/mwh/currents/winds
        nest as missing — silently losing the authoritative source in exactly the
        evidence-panel disagreement beat CLAUDE.md calls the demo's centrepiece. See
        ``docs/DECISIONS.md`` D11.
        """
        blob = "|".join([
            urlPath, ",".join(sorted(raw_vars)),
            ",".join(f"{x:.4f}" for x in bbox),
        ])
        return hashlib.sha1(blob.encode()).hexdigest()[:20]

    def _fetch_grid_bytes(
        self, product: str, urlPath: str, raw_vars: Sequence[str],
        bbox: tuple[float, float, float, float],
        time_start: datetime | None, time_end: datetime | None,
    ) -> _BlobFetch:
        sub_source = f"{self.source_id}_{product}"
        key = self._binary_key(urlPath, raw_vars, bbox)

        if is_fixture():
            path = binary_path(sub_source, key, ".nc")
            if path is None:
                raise SourceError(
                    sub_source,
                    f"no frozen fixture blob for key={key} (product={product}, url_path={urlPath}); "
                    f"run scripts/freeze_fixtures.py in live mode first",
                )
            return _BlobFetch(path=path, acquired_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
                               from_fixture=True, from_cache=False)

        cached = binary_path(sub_source, key, ".nc")
        if cached is not None and (time.time() - cached.stat().st_mtime) <= self.cache_ttl_s:
            return _BlobFetch(path=cached, acquired_at=datetime.fromtimestamp(cached.stat().st_mtime, tz=UTC),
                               from_fixture=False, from_cache=True)

        url = f"{THREDDS}/ncss/grid/{urlPath}"
        minlon, minlat, maxlon, maxlat = bbox
        params: dict[str, Any] = {
            "var": ",".join(raw_vars),
            "west": minlon, "south": minlat, "east": maxlon, "north": maxlat,
            "accept": "netcdf3",
        }
        if time_start is not None:
            params["time_start"] = _iso_z(time_start)
        if time_end is not None:
            params["time_end"] = _iso_z(time_end)
        headers = {"Referer": default_referer(url)}

        last_exc: Exception | None = None
        for attempt in range(3):
            t0 = time.perf_counter()
            try:
                resp = client().get(url, params=params, headers=headers)
                if resp.status_code == 400:
                    # A NCSS 400 on a grid request is almost always "requested bbox/time
                    # does not intersect the published grid" (confirmed live for chl over
                    # this region's bbox) — a permanent, non-transient client error.
                    # Retrying it three times with backoff just burns the health-check
                    # budget; fail fast instead.
                    raise SourceError(
                        sub_source,
                        f"NCSS 400 for {product!r} (likely requested bbox/time falls outside "
                        f"the published grid): {resp.text[:200]!r}",
                        status=400,
                    )
                if resp.status_code == 503:
                    raise SourceError(sub_source, "503 Service Unavailable from NCSS", status=503)
                resp.raise_for_status()
                data = resp.content
                ctype = resp.headers.get("content-type", "")
                if len(data) < 400 and ("html" in ctype or b"<html" in data[:200].lower()):
                    raise SourceError(
                        sub_source,
                        f"NCSS returned an HTML error page instead of NetCDF for {product!r} "
                        f"(possibly requested bbox does not intersect the published grid): "
                        f"{data[:200]!r}",
                    )
                latency = int((time.perf_counter() - t0) * 1000)
                log.info("%s: fetched %d bytes for %s in %d ms", sub_source, len(data), urlPath, latency)
                return _BlobFetch(
                    path=cache_binary(sub_source, key, data, ".nc"),
                    acquired_at=utcnow(), from_fixture=False, from_cache=False,
                )
            except SourceError as exc:
                if exc.status == 400:
                    raise
                last_exc = exc
                log.warning("%s fetch attempt %d failed: %s", sub_source, attempt + 1, exc)
                if attempt < 2:
                    time.sleep(0.6 * (2**attempt))
            except Exception as exc:  # noqa: BLE001 - adapters must not leak transport errors
                last_exc = exc
                log.warning("%s fetch attempt %d failed: %s", sub_source, attempt + 1, exc)
                if attempt < 2:
                    time.sleep(0.6 * (2**attempt))

        if cached is not None:
            log.warning("%s: serving stale cached blob after fetch failure", sub_source)
            return _BlobFetch(path=cached, acquired_at=datetime.fromtimestamp(cached.stat().st_mtime, tz=UTC),
                               from_fixture=False, from_cache=True)
        raise SourceError(sub_source, f"NCSS fetch failed for {url}: {last_exc}")

    # -- grid decode -------------------------------------------------------------------

    @staticmethod
    def _history_for(ds: xr.Dataset) -> str | None:
        for name in ds.data_vars:
            h = ds[name].attrs.get("history")
            if h:
                return str(h)
        h = ds.attrs.get("history") or ds.attrs.get("History")
        return str(h) if h else None

    def _compute_canonical(
        self, ds: xr.Dataset, canon: str, ti: int, xdim: str, ydim: str, tdim: str | None, zdim: str | None
    ) -> np.ndarray | None:
        if canon == "current_direction":
            if "U" not in ds.variables or "V" not in ds.variables:
                return None
            u = _select_2d(ds, "U", ti, xdim, ydim, tdim, zdim)
            v = _select_2d(ds, "V", ti, xdim, ydim, tdim, zdim)
            return _bearing_to(u, v)
        if canon == "wind_direction":
            if "WSXM" not in ds.variables or "WSYM" not in ds.variables:
                return None
            u = _select_2d(ds, "WSXM", ti, xdim, ydim, tdim, zdim)
            v = _select_2d(ds, "WSYM", ti, xdim, ydim, tdim, zdim)
            return _met_from(u, v)
        raw = _CANON_TO_RAW_SINGLE.get(canon)
        if raw is None or raw not in ds.variables:
            return None
        return _select_2d(ds, raw, ti, xdim, ydim, tdim, zdim)

    def _grid_slices(
        self, product: str, requested_canon: Sequence[str], raw_vars: Sequence[str],
        bbox: tuple[float, float, float, float], time_start: datetime | None, time_end: datetime | None,
        file_date: date, url_path: str,
    ) -> list[GridSlice]:
        blob = self._fetch_grid_bytes(product, url_path, raw_vars, bbox, time_start, time_end)
        ds = xr.open_dataset(blob.path)
        try:
            xdim, ydim, tdim, zdim = _axis_dims(ds)
            lats = np.asarray(ds[ydim].values, dtype=float)
            lons = np.asarray(ds[xdim].values, dtype=float)
            history = self._history_for(ds)
            n_times = int(ds.sizes[tdim]) if tdim is not None else 1

            _prefix, _default_vars, resolution_m = PRODUCTS[product]
            issued_at = datetime(file_date.year, file_date.month, file_date.day, tzinfo=UTC)
            valid_to = issued_at + _VALID_SPAN[product]
            note_bits = [f"INCOIS OSF {product} nest, file date {file_date.isoformat()}"]
            if history:
                note_bits.append(f"model: {history}")
            if blob.from_fixture:
                note_bits.append("replayed from frozen fixture (FORESHORE_MODE=fixture)")
            elif blob.from_cache:
                note_bits.append("served from local snapshot cache")
            prov = Provenance(
                source_id=f"{self.source_id}_{product}",
                source_name=f"{self.source_name} — {product}",
                authority=self.authority,
                url=f"{THREDDS}/ncss/grid/{url_path}",
                acquired_at=blob.acquired_at,
                issued_at=issued_at,
                valid_from=issued_at,
                valid_to=valid_to,
                spatial_resolution_m=resolution_m,
                temporal_resolution_s=_TEMPORAL_RES_S[product],
                is_derived=False,
                notes="; ".join(note_bits),
            )

            out: list[GridSlice] = []
            for ti in range(n_times):
                if tdim is not None:
                    vt = _dt64_to_aware(ds[tdim].values[ti])
                else:
                    vt = issued_at
                canon_arrays: dict[str, np.ndarray] = {}
                for canon in requested_canon:
                    arr = self._compute_canonical(ds, canon, ti, xdim, ydim, tdim, zdim)
                    if arr is not None:
                        canon_arrays[canon] = arr
                out.append(GridSlice(
                    product=product, variables=canon_arrays, lats=lats, lons=lons,
                    valid_time=vt, file_date=file_date, local_path=blob.path,
                    history=history, provenance=prov,
                ))
            return out
        finally:
            ds.close()

    # -- point bbox helper ---------------------------------------------------------

    @staticmethod
    def _point_bbox(lat: float, lon: float, pad: float = 0.5) -> tuple[float, float, float, float]:
        return (max(-180.0, lon - pad), max(-90.0, lat - pad), min(180.0, lon + pad), min(90.0, lat + pad))

    # -- public contract -------------------------------------------------------------

    def slice(
        self, product: str, *, variables: Sequence[str] | None = None,
        at: datetime | None = None, bbox: tuple[float, float, float, float] | None = None,
    ) -> GridSlice:
        if product not in PRODUCTS:
            raise SourceError(self.source_id, f"unknown product {product!r}; choose from {sorted(PRODUCTS)}")
        requested_canon, raw_vars = self._raw_vars_for(product, variables)
        bbox_use = bbox or self.region.bbox
        file_date = self._resolve_file_date(product, at)
        url_path = self._catalog_map(product)[file_date]
        ts = te = None
        if at is not None:
            ts, te = _aware(at) - timedelta(hours=6), _aware(at) + timedelta(hours=6)
        slices = self._grid_slices(product, requested_canon, raw_vars, bbox_use, ts, te, file_date, url_path)
        if not slices:
            raise SourceError(self.source_id, f"no timesteps decoded for {product!r} file {url_path!r}")
        target = _aware(at) if at is not None else utcnow()
        return min(slices, key=lambda gs: abs((gs.valid_time - target).total_seconds()))

    def point(
        self, product: str, lat: float, lon: float, *,
        at: datetime | None = None, variables: Sequence[str] | None = None,
    ) -> list[Observation]:
        gs = self.slice(product, variables=variables, at=at, bbox=self._point_bbox(lat, lon))
        out: list[Observation] = []
        for canon, arr in gs.variables.items():
            val, dist = gs.value_at(canon, lat, lon)
            if val is None:
                continue
            out.append(self.observe(
                canon, val, UNITS[canon], lat, lon, gs.valid_time, gs.provenance,
                grid_cell_distance_m=round(dist, 1), file_date=gs.file_date.isoformat(),
                product=product, model=gs.history, source_variable=_SOURCE_VAR_LABEL[canon],
            ))
        return out

    def series(
        self, product: str, lat: float, lon: float, *,
        variables: Sequence[str] | None = None, hours: int = 48,
    ) -> list[Observation]:
        if product not in PRODUCTS:
            raise SourceError(self.source_id, f"unknown product {product!r}; choose from {sorted(PRODUCTS)}")
        requested_canon, raw_vars = self._raw_vars_for(product, variables)
        file_date = self._resolve_file_date(product, None)
        url_path = self._catalog_map(product)[file_date]
        start = utcnow()
        end = start + timedelta(hours=max(1, int(hours)))
        bbox_use = self._point_bbox(lat, lon)
        slices = self._grid_slices(product, requested_canon, raw_vars, bbox_use, start, end, file_date, url_path)
        slices.sort(key=lambda gs: gs.valid_time)
        out: list[Observation] = []
        for gs in slices:
            for canon in requested_canon:
                val, dist = gs.value_at(canon, lat, lon)
                if val is None:
                    continue
                out.append(self.observe(
                    canon, val, UNITS[canon], lat, lon, gs.valid_time, gs.provenance,
                    grid_cell_distance_m=round(dist, 1), file_date=gs.file_date.isoformat(),
                    product=product, model=gs.history, source_variable=_SOURCE_VAR_LABEL[canon],
                ))
        return out

    # -- base Source contract ---------------------------------------------------------

    def fetch(
        self, *, product: str = "wave", lat: float | None = None, lon: float | None = None,
        at: datetime | None = None, variables: Sequence[str] | None = None, **_: Any,
    ) -> Any:
        from .base import FetchResult

        lat = self.region.centre[0] if lat is None else lat
        lon = self.region.centre[1] if lon is None else lon
        gs = self.slice(product, variables=variables, at=at, bbox=self._point_bbox(lat, lon))
        return FetchResult(
            payload={
                "product": product, "lat": lat, "lon": lon,
                "file_date": gs.file_date.isoformat(), "valid_time": gs.valid_time.isoformat(),
            },
            url=gs.provenance.url, key=f"{product}-{gs.file_date.isoformat()}",
            acquired_at=gs.provenance.acquired_at,
        )

    def parse(self, raw: Any, *, lat: float | None = None, lon: float | None = None, **_: Any) -> list[Observation]:
        payload = raw.payload if isinstance(raw.payload, dict) else {}
        product = payload.get("product", "wave")
        lat = payload.get("lat") if lat is None else lat
        lon = payload.get("lon") if lon is None else lon
        lat = self.region.centre[0] if lat is None else lat
        lon = self.region.centre[1] if lon is None else lon
        return self.point(product, lat, lon)

    def health(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        lat, lon = self.region.centre
        products_info: dict[str, Any] = {}
        primary_ok = False
        for product in PRODUCTS:
            p_t0 = time.perf_counter()
            try:
                dates = self.catalog_dates(product)
                latest = dates[-1] if dates else None
                lag_days = (utcnow().date() - latest).days if latest else None
                obs = self.point(product, lat, lon)
                ok = bool(obs)
                products_info[product] = {
                    "ok": ok,
                    "latest": latest.isoformat() if latest else None,
                    "lag_days": lag_days,
                    "vars": sorted({o.variable for o in obs}),
                    "count": len(obs),
                    "latency_ms": int((time.perf_counter() - p_t0) * 1000),
                    "error": None if ok else "no valid (non-fill) data at region centre for the latest run",
                }
                if product == "wave" and ok:
                    primary_ok = True
            except Exception as exc:  # noqa: BLE001
                products_info[product] = {
                    "ok": False, "latest": None, "lag_days": None, "vars": [], "count": 0,
                    "latency_ms": int((time.perf_counter() - p_t0) * 1000),
                    "error": f"{type(exc).__name__}: {exc}",
                }
        return {
            "source_id": self.source_id,
            "ok": primary_ok,
            "count": sum(p["count"] for p in products_info.values()),
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "issued_at": products_info.get("wave", {}).get("latest"),
            "freshness": None,
            "resolution_m": self.spatial_resolution_m,
            "error": None if primary_ok else "authoritative wave product unavailable",
            "products": products_info,
        }


__all__ = ["THREDDS", "PRODUCTS", "UNITS", "GridSlice", "IncoisThredds"]
