"""INCOIS ERDDAP adapter — gridded Argo 10-day objective analysis (``incois_argo_10d_VAM``).

Keyless griddap dataset. Probed live on 2026-08-31, in this order, before writing any
parser, exactly as the brief prescribes:

1. ``.das`` (metadata/attributes) — real dimension and variable names found:
   dimensions ``time, ZAX, latitude, longitude`` (in that order — every data variable's
   axis order is ``[time][ZAX][latitude][longitude]``); data variables ``TEMP``
   (temperature, unit the dataset itself labels ``"degs"``), ``TERR`` (temperature
   relative error), ``SAL`` (practical salinity, ``PSU``), ``SERR`` (salinity relative
   error). ``NC_GLOBAL`` carries ``time_coverage_start = 2004-01-10T00:00:00Z``,
   ``time_coverage_end = 2026-07-30T00:00:00Z`` (813 ten-day steps), ``geospatial_lat_min/
   max = -29.5/29.5`` (1° resolution), ``geospatial_lon_min/max = 30.5/119.5`` (1°
   resolution). ``_FillValue``/``missing_value`` = ``-9999.0`` for every data variable,
   but missing cells are actually returned as the literal string ``"NaN"`` in ``.csv``
   output, not ``-9999`` — both are treated as "no data" here.
2. ``.dds`` (structure) — confirms grid sizes: ``time=813, ZAX=24, latitude=60,
   longitude=90``.
3. ``.csv`` point subsets, e.g.
   ``incois_argo_10d_VAM.csv?TEMP[(last)][(5):(2000)][(9.2876)][(79.3129)],SAL[...]``.
   Two findings that shaped this module:

   - Parentheses/brackets in the OPeNDAP-style constraint expression are **not**
     standard query parameters (no ``&``/``=``), and the ERDDAP Tomcat front end 400s on
     literal, unescaped ``[ ] ( )`` in the request line (``Invalid character found in
     the request target``) even though they render fine pasted into a browser address
     bar. This module percent-encodes the whole constraint expression and appends it to
     the URL itself rather than passing it as ``params`` to ``Source.get`` — the
     constraint is one opaque expression, not key/value pairs.
   - ERDDAP resolves point/value constraints (``(9.2876)``, ``(5):(2000)``,
     ``(2020-01-01T00:00:00Z):(...)``) to the **nearest actual grid coordinate itself**
     — passing the exact query lat/lon back gets the snapped grid centre echoed in the
     response's own ``latitude``/``longitude`` columns. No manual nearest-neighbour
     snapping is done here; the grid point actually used is read back from the response
     and carried in every Observation's qualifiers (``grid_lat``/``grid_lon`` vs.
     ``requested_lat``/``requested_lon``).

   Rameswaram (9.2876, 79.3129) snaps to grid cell (9.5, 79.5). At the most recent step
   (2026-07-30) that cell has real, non-``NaN`` TEMP/SAL at 5 m and 10 m and ``NaN`` at
   every deeper level (20 m through 2000 m) — physically correct, not a bug: Palk Bay is
   only a few metres deep and the 1° VAM grid cell straddling Rameswaram is dominated by
   that shallow water, so the objective analysis simply has no deep-water signal there.
   The acceptance point below is therefore used as-is (no fallback to a different point
   was needed); ``profile()`` silently skips ``NaN`` levels rather than fabricating them.

This is the slowest of the three sources in this batch (external ERDDAP round-trip per
distinct query) — cached at ``cache_ttl_s = 86400.0`` and every query is bounded to a
handful of variables x a few hundred rows at most.

A second adapter, :class:`IncoisOceansat`, shares this ERDDAP host and the parsing
helpers below (``_csv_url`` generalised to take a dataset id, ``_parse_csv_text``,
``_parse_das``, ``_parse_dds_dims``, ``_is_missing``, ``_iso_z``, ``_parse_erddap_time``)
for ISRO's Oceansat-2 Ocean Colour Monitor (``incois_oceansat2_datasets``) — a closed
2011-2020 archive, see that class's own docstring.
"""

from __future__ import annotations

import csv
import io
import re
import time
from datetime import datetime, timedelta
from typing import Any, Sequence
from urllib.parse import quote

from ..models import UTC, Observation
from .base import FetchResult, Source

ERDDAP = "https://erddap.incois.gov.in/erddap"
DATASET = "incois_argo_10d_VAM"

_DIM_NAMES = ("time", "ZAX", "latitude", "longitude")
_DATA_VARS = ("TEMP", "TERR", "SAL", "SERR")

#: Bound on how much history one `timeseries()` call may span. At the dataset's ~10-day
#: cadence this is ~330 rows — comfortably "a few hundred", never more.
MAX_TIMESERIES_SPAN_DAYS = 3300.0


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


def _csv_url(dataset: str, variables: Sequence[str], dims_expr: str) -> str:
    """Build a percent-encoded griddap ``.csv`` URL for any dataset on this ERDDAP.

    Generalised from a hardcoded ``DATASET`` (Argo) so :class:`IncoisOceansat` can reuse
    it rather than duplicating the Tomcat-escaping logic documented in this module's
    docstring. Both call sites (``IncoisArgo.profile``/``timeseries`` and
    ``IncoisOceansat.chlorophyll_series``) now pass their own dataset id explicitly.
    """
    query = ",".join(f"{v}{dims_expr}" for v in variables)
    return f"{ERDDAP}/griddap/{dataset}.csv?{quote(query, safe='')}"


def _parse_csv_text(text: str) -> tuple[list[str], list[str], list[dict[str, str]]]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return [], [], []
    rows = list(csv.reader(io.StringIO("\n".join(lines))))
    cols = rows[0]
    units = rows[1] if len(rows) > 1 else ["" for _ in cols]
    data = [dict(zip(cols, r)) for r in rows[2:]]
    return cols, units, data


def _is_missing(s: str | None) -> bool:
    return s is None or s.strip() == "" or s.strip().lower() == "nan"


class IncoisArgo(Source):
    """INCOIS gridded Argo 10-day objective analysis (``incois_argo_10d_VAM``), ERDDAP."""

    source_id = "incois_argo"
    source_name = "INCOIS gridded Argo 10-day objective analysis (incois_argo_10d_VAM)"
    authority = "INCOIS"
    validity = timedelta(days=15)
    cache_ttl_s = 86400.0
    #: ~1° grid, i.e. ~111 km meridionally (coarser east-west away from the equator) —
    #: an approximation, documented as such, never presented as a precise footprint.
    spatial_resolution_m = 111_000.0

    # -- transport ---------------------------------------------------------------------

    def _das_fetch(self) -> FetchResult:
        return self.get(f"{ERDDAP}/griddap/{DATASET}.das")

    def _dds_fetch(self) -> FetchResult:
        return self.get(f"{ERDDAP}/griddap/{DATASET}.dds")

    # -- metadata ------------------------------------------------------------------

    def metadata(self) -> dict[str, Any]:
        das_raw = self._das_fetch()
        dds_raw = self._dds_fetch()
        das = _parse_das(das_raw.text)
        sizes = _parse_dds_dims(dds_raw.text)

        dims = {name: dict(das[name]) for name in _DIM_NAMES if name in das}
        for name, size in sizes.items():
            if name in dims:
                dims[name]["size"] = size
        variables = {name: das[name] for name in _DATA_VARS if name in das}
        global_attrs = das.get("NC_GLOBAL", {})

        def _range(dim: str) -> tuple[float | None, float | None]:
            r = dims.get(dim, {}).get("actual_range")
            if isinstance(r, list) and len(r) == 2:
                return float(r[0]), float(r[1])
            return None, None

        return {
            "dataset_id": DATASET,
            "dimension_order": list(_DIM_NAMES),
            "dimensions": dims,
            "variables": variables,
            "global": global_attrs,
            "time_coverage_start": global_attrs.get("time_coverage_start"),
            "time_coverage_end": global_attrs.get("time_coverage_end"),
            "lat_range": _range("latitude"),
            "lon_range": _range("longitude"),
            "depth_range": _range("ZAX"),
            "lat_resolution_deg": global_attrs.get("geospatial_lat_resolution"),
            "lon_resolution_deg": global_attrs.get("geospatial_lon_resolution"),
        }

    # -- typed outputs -----------------------------------------------------------------

    def _rows_to_observations(
        self,
        raw: FetchResult,
        *,
        requested_lat: float,
        requested_lon: float,
        note: str,
    ) -> list[Observation]:
        cols, units, rows = _parse_csv_text(raw.text)
        if not rows:
            return []
        unit_of = dict(zip(cols, units))
        out: list[Observation] = []
        for row in rows:
            depth_s = row.get("ZAX")
            if _is_missing(depth_s):
                continue
            depth_m = float(depth_s)
            grid_lat = float(row["latitude"]) if not _is_missing(row.get("latitude")) else None
            grid_lon = float(row["longitude"]) if not _is_missing(row.get("longitude")) else None
            valid_time = _parse_erddap_time(row.get("time")) or raw.acquired_at
            lat = grid_lat if grid_lat is not None else requested_lat
            lon = grid_lon if grid_lon is not None else requested_lon
            prov = self.provenance(raw, issued_at=valid_time, valid_from=valid_time, notes=note)

            temp_s, terr_s = row.get("TEMP"), row.get("TERR")
            if not _is_missing(temp_s):
                out.append(
                    self.observe(
                        "subsurface_temperature", float(temp_s), unit_of.get("TEMP", "degs"),
                        lat, lon, valid_time=valid_time, provenance=prov,
                        depth_m=depth_m, requested_lat=requested_lat, requested_lon=requested_lon,
                        grid_lat=grid_lat, grid_lon=grid_lon,
                        relative_error=(float(terr_s) if not _is_missing(terr_s) else None),
                    )
                )
            sal_s, serr_s = row.get("SAL"), row.get("SERR")
            if not _is_missing(sal_s):
                out.append(
                    self.observe(
                        "subsurface_salinity", float(sal_s), unit_of.get("SAL", "PSU"),
                        lat, lon, valid_time=valid_time, provenance=prov,
                        depth_m=depth_m, requested_lat=requested_lat, requested_lon=requested_lon,
                        grid_lat=grid_lat, grid_lon=grid_lon,
                        relative_error=(float(serr_s) if not _is_missing(serr_s) else None),
                    )
                )
        return out

    def profile(self, lat: float, lon: float, when: datetime | None = None) -> list[Observation]:
        """Temperature and salinity down the full depth axis at one point and time."""
        meta = self.metadata()
        depth_min, depth_max = meta["depth_range"]
        depth_min = depth_min if depth_min is not None else 0.0
        depth_max = depth_max if depth_max is not None else 2000.0
        time_expr = "(last)" if when is None else f"({_iso_z(when)})"
        dims_expr = f"[{time_expr}][({depth_min}):({depth_max})][({lat})][({lon})]"
        url = _csv_url(DATASET, _DATA_VARS, dims_expr)
        raw = self.get(url, key=f"{DATASET}:profile:{lat}:{lon}:{time_expr}")
        note = (
            "INCOIS gridded Argo 10-day objective analysis (VAM), ~1° grid cell "
            "nearest-neighbour value — not an in-situ profile at the exact point."
        )
        return self._rows_to_observations(raw, requested_lat=lat, requested_lon=lon, note=note)

    def timeseries(
        self, lat: float, lon: float, depth_m: float, start: datetime, end: datetime
    ) -> list[Observation]:
        """Temperature/salinity at one depth and point across a bounded time span."""
        if end <= start:
            raise ValueError("timeseries: end must be after start")
        span_days = (end - start).total_seconds() / 86400.0
        note = (
            "INCOIS gridded Argo 10-day objective analysis (VAM), ~1° grid cell "
            "nearest-neighbour value, multi-decadal productivity diagnostic input."
        )
        if span_days > MAX_TIMESERIES_SPAN_DAYS:
            end = start + timedelta(days=MAX_TIMESERIES_SPAN_DAYS)
            note += f" Time range clamped to {MAX_TIMESERIES_SPAN_DAYS:.0f} days to bound the request."
        dims_expr = f"[({_iso_z(start)}):({_iso_z(end)})][({depth_m})][({lat})][({lon})]"
        url = _csv_url(DATASET, _DATA_VARS, dims_expr)
        key = f"{DATASET}:timeseries:{lat}:{lon}:{depth_m}:{_iso_z(start)}:{_iso_z(end)}"
        raw = self.get(url, key=key)
        return self._rows_to_observations(raw, requested_lat=lat, requested_lon=lon, note=note)

    # -- generic Source contract --------------------------------------------------------

    def parse(self, raw: FetchResult, **kw: Any) -> list[Observation]:
        """Parses a ``.csv`` point/series ``FetchResult``. A ``.das``/``.dds`` metadata
        fetch carries no data values and parses to an empty list — a valid outcome."""
        if not raw.url.endswith(".csv") and ".csv?" not in raw.url:
            return []
        lat0, lon0 = self.region.centre
        note = "INCOIS gridded Argo 10-day objective analysis (VAM)."
        return self._rows_to_observations(raw, requested_lat=lat0, requested_lon=lon0, note=note)

    def fetch(self, **kwargs: Any) -> FetchResult:
        return self._das_fetch()

    def health(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            meta = self.metadata()
            latency_ms = int((time.perf_counter() - t0) * 1000)
            latest = _parse_erddap_time(meta.get("time_coverage_end"))
            return {
                "source_id": self.source_id,
                "ok": True,
                "count": len(meta.get("variables", {})),
                "latency_ms": latency_ms,
                "issued_at": latest.isoformat() if latest else meta.get("time_coverage_end"),
                "freshness": None,
                "resolution_m": self.spatial_resolution_m,
                "error": None,
                "time_coverage_start": meta.get("time_coverage_start"),
                "time_coverage_end": meta.get("time_coverage_end"),
                "grid": {
                    "lat_range": meta.get("lat_range"),
                    "lon_range": meta.get("lon_range"),
                    "depth_range": meta.get("depth_range"),
                },
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "source_id": self.source_id,
                "ok": False,
                "count": 0,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "issued_at": None,
                "freshness": None,
                "resolution_m": self.spatial_resolution_m,
                "error": f"{type(exc).__name__}: {exc}",
            }


DATASET_OCEANSAT = "incois_oceansat2_datasets"

_OCEANSAT_DIM_NAMES = ("time", "latitude", "longitude")
_OCEANSAT_DATA_VARS = ("CHL", "KD490", "TSM")

#: Verified live 2026-09-06 against this dataset's own NC_GLOBAL time_coverage_start/end.
#: Oceansat-2 OCM was decommissioned and this ERDDAP dataset was never extended past its
#: mission life, so unlike ``incois_argo_10d_VAM`` (which grows every 10 days) these
#: bounds are fixed -- a closed archive, not a moving window. Cross-checked against a
#: live ``metadata()`` call rather than assumed at every call site.
OCEANSAT_COVERAGE_START = datetime(2011, 2, 2, tzinfo=UTC)
OCEANSAT_COVERAGE_END = datetime(2020, 5, 1, tzinfo=UTC)

#: 1 degree of latitude ~= 111_320 m. Used only to convert the dataset's own declared
#: ``geospatial_lat_resolution`` into metres -- never to invent a resolution.
_DEG_TO_M = 111_320.0

#: Documented fallback if ``geospatial_lat_resolution`` is ever absent from a live
#: ``.das`` response (has not happened in probing): 0.03880722027396 deg * 111_320 m/deg
#: ~= 4319.8 m, i.e. ~4_320 m. ``IncoisOceansat._resolution_m`` reads the live attribute
#: first and only falls back to this constant when the attribute is missing.
OCEANSAT_FALLBACK_RESOLUTION_M = 4_320.0


def _clamp_to_coverage(
    start: datetime, end: datetime, coverage_start: datetime, coverage_end: datetime
) -> tuple[datetime, datetime, bool]:
    """Clamp a requested ``[start, end]`` window into a dataset's own declared coverage.

    Pure function, no I/O -- exercised directly by ``test_incois_oceansat.py`` without a
    live ``metadata()`` call. Three cases:

    * the window is entirely inside coverage -> returned unchanged, ``clamped=False``.
    * the window overlaps coverage on one or both sides -> each overshooting edge is
      pulled in to the coverage boundary, ``clamped=True``.
    * the window misses coverage entirely (wholly before or wholly after) -> collapsed
      to a single instant at the nearest boundary rather than left inverted (an
      ``end < start`` range is not a "recent view of an old archive", it is nonsense,
      and would otherwise 400 against ERDDAP instead of failing here with a clear flag).

    Accepts naive or aware datetimes for ``start``/``end`` (naive is treated as UTC,
    matching ``_iso_z``); ``coverage_start``/``coverage_end`` are always aware already.
    """
    start = start if start.tzinfo else start.replace(tzinfo=UTC)
    end = end if end.tzinfo else end.replace(tzinfo=UTC)
    if end < coverage_start:
        return coverage_start, coverage_start, True
    if start > coverage_end:
        return coverage_end, coverage_end, True
    clamped = False
    if start < coverage_start:
        start = coverage_start
        clamped = True
    if end > coverage_end:
        end = coverage_end
        clamped = True
    return start, end, clamped


class IncoisOceansat(Source):
    """INCOIS ERDDAP mirror of ISRO's Oceansat-2 Ocean Colour Monitor (``incois_oceansat2_datasets``).

    Exists because INCOIS's own live chlorophyll grid (``osf/chl``, ``IncoisThredds``)
    does not cover this coast at all -- it is a Pacific Islands Countries product, lon
    129.98-215.02 E, nowhere near Palk Bay/Gulf of Mannar. FORESHORE's "why has fish
    productivity declined?" diagnostic needs a multi-year chlorophyll record over this
    coast, and this dataset is the one that actually has it: griddap dimensions
    ``time[3377], latitude[717], longitude[1317]``, data variables ``CHL`` (mg/m3),
    ``KD490``, ``TSM``, grid extent lat 0.107-27.893, lon 46.683-99.317 -- India and the
    Arabian Sea/Bay of Bengal are well inside it. It is also, for a problem statement
    filed by ISRO/Department of Space, better provenance than the NOAA CoastWatch
    fallback this codebase also carries (``oceancolour.py``): this is ISRO's own
    Oceansat-2 OCM instrument, served from INCOIS's own ERDDAP.

    **This is a closed archive, not a live feed.** ``time_coverage_start`` is
    2011-02-02, ``time_coverage_end`` is 2020-05-01 -- the mission's operational life --
    and nothing past that date will ever appear here. Every value this class emits is a
    historical record for a multi-year trend diagnostic; none of it may be presented as
    current conditions. ``Source.provenance``'s own freshness computation makes this
    hard to get wrong by accident: with ``validity = timedelta(days=30)``, any
    Observation's ``valid_to`` sits at most 30 days after a timestep no later than
    2020-05-01, so ``freshness_at(utcnow())`` reads ``"expired"`` for every single value
    this class has ever returned or ever will, from the moment more than 30 days after
    2026-09-06 (in practice, from 2020 onward) -- exactly the outcome wanted for a
    closed-archive source.

    Reuses this module's ``_csv_url`` (generalised to take a dataset id), ``_parse_csv_text``,
    ``_parse_das``, ``_parse_dds_dims``, ``_is_missing``, ``_iso_z`` and
    ``_parse_erddap_time`` rather than duplicating the ERDDAP/Tomcat percent-encoding
    findings this module's own docstring already records.
    """

    source_id = "incois_oceansat2"
    source_name = "INCOIS / ISRO Oceansat-2 Ocean Colour Monitor (incois_oceansat2_datasets)"
    authority = "ISRO/NRSC"
    validity = timedelta(days=30)
    cache_ttl_s = 86400.0
    #: Documented fallback only -- see ``OCEANSAT_FALLBACK_RESOLUTION_M`` above.
    #: ``_resolution_m`` prefers the live-derived value whenever ``metadata()`` succeeds.
    spatial_resolution_m = OCEANSAT_FALLBACK_RESOLUTION_M

    # -- transport ---------------------------------------------------------------------

    def _das_fetch(self) -> FetchResult:
        return self.get(f"{ERDDAP}/griddap/{DATASET_OCEANSAT}.das")

    def _dds_fetch(self) -> FetchResult:
        return self.get(f"{ERDDAP}/griddap/{DATASET_OCEANSAT}.dds")

    # -- metadata ------------------------------------------------------------------

    def metadata(self) -> dict[str, Any]:
        das_raw = self._das_fetch()
        dds_raw = self._dds_fetch()
        das = _parse_das(das_raw.text)
        sizes = _parse_dds_dims(dds_raw.text)

        dims = {name: dict(das[name]) for name in _OCEANSAT_DIM_NAMES if name in das}
        for name, size in sizes.items():
            if name in dims:
                dims[name]["size"] = size
        variables = {name: das[name] for name in _OCEANSAT_DATA_VARS if name in das}
        global_attrs = das.get("NC_GLOBAL", {})

        def _range(dim: str) -> tuple[float | None, float | None]:
            r = dims.get(dim, {}).get("actual_range")
            if isinstance(r, list) and len(r) == 2:
                return float(r[0]), float(r[1])
            return None, None

        lat_range = _range("latitude")
        lat_ascending = (
            lat_range[0] is not None
            and lat_range[1] is not None
            and lat_range[0] <= lat_range[1]
        )

        return {
            "dataset_id": DATASET_OCEANSAT,
            "dimension_order": list(_OCEANSAT_DIM_NAMES),
            "dimensions": dims,
            "variables": variables,
            "global": global_attrs,
            "time_coverage_start": global_attrs.get("time_coverage_start"),
            "time_coverage_end": global_attrs.get("time_coverage_end"),
            "lat_range": lat_range,
            "lon_range": _range("longitude"),
            "lat_resolution_deg": global_attrs.get("geospatial_lat_resolution"),
            "lon_resolution_deg": global_attrs.get("geospatial_lon_resolution"),
            "lat_ascending": lat_ascending,
        }

    def _resolution_m(self, meta: dict[str, Any]) -> float:
        """Metres per grid cell, derived from the dataset's own declared resolution.

        ``geospatial_lat_resolution`` (degrees) x ``_DEG_TO_M`` -- never a hardcoded
        constant on the main path. Falls back to ``OCEANSAT_FALLBACK_RESOLUTION_M`` only
        when the attribute itself is missing from a live ``.das`` response.
        """
        lat_res = meta.get("lat_resolution_deg")
        if isinstance(lat_res, (int, float)):
            return float(lat_res) * _DEG_TO_M
        return OCEANSAT_FALLBACK_RESOLUTION_M

    # -- typed outputs -----------------------------------------------------------------

    def _rows_to_observations(
        self,
        raw: FetchResult,
        *,
        requested_lat: float,
        requested_lon: float,
        resolution_m: float,
        time_range_clamped: bool,
        note: str,
    ) -> list[Observation]:
        cols, units, rows = _parse_csv_text(raw.text)
        if not rows:
            return []
        out: list[Observation] = []
        for row in rows:
            chl_s = row.get("CHL")
            if _is_missing(chl_s):
                continue
            grid_lat = float(row["latitude"]) if not _is_missing(row.get("latitude")) else None
            grid_lon = float(row["longitude"]) if not _is_missing(row.get("longitude")) else None
            valid_time = _parse_erddap_time(row.get("time")) or raw.acquired_at
            lat = grid_lat if grid_lat is not None else requested_lat
            lon = grid_lon if grid_lon is not None else requested_lon
            prov = self.provenance(
                raw,
                issued_at=valid_time,
                valid_from=valid_time,
                spatial_resolution_m=resolution_m,
                notes=note,
                is_derived=False,
            )
            out.append(
                self.observe(
                    "chlorophyll_a", float(chl_s), "mg/m^3", lat, lon,
                    valid_time=valid_time, provenance=prov,
                    requested_lat=requested_lat, requested_lon=requested_lon,
                    grid_lat=grid_lat, grid_lon=grid_lon,
                    time_range_clamped=time_range_clamped,
                )
            )
        return out

    def _series_key(self, lat: float, lon: float, start: datetime, end: datetime) -> str:
        """Cache/fixture key for one ``chlorophyll_series`` call.

        Deterministic in its four arguments alone — no wall-clock instant baked in. This
        repeats, deliberately, the fix ``test_incois_thredds_key.py`` locks in for
        ``IncoisThredds._binary_key``: a key derived from "now" can never match a frozen
        fixture on replay, which silently dropped that source from every live query. Kept
        as its own method so the determinism claim is directly testable without a fetch.
        """
        return f"{DATASET_OCEANSAT}:chl:{lat}:{lon}:{_iso_z(start)}:{_iso_z(end)}"

    def chlorophyll_series(
        self, lat: float, lon: float, *, start: datetime, end: datetime
    ) -> list[Observation]:
        """Chlorophyll-a at one point across a time span, clamped to this closed archive.

        ``start``/``end`` outside 2011-02-02..2020-05-01 are clamped to the dataset's own
        coverage rather than sent through as-is (which would either 400 against ERDDAP or
        silently return nothing) -- see ``_clamp_to_coverage``. Every Observation carries
        ``qualifiers["time_range_clamped"]`` so a caller can tell a clamped answer from an
        exact one without re-deriving the coverage window itself.
        """
        if end <= start:
            raise ValueError("chlorophyll_series: end must be after start")
        clamped_start, clamped_end, was_clamped = _clamp_to_coverage(
            start, end, OCEANSAT_COVERAGE_START, OCEANSAT_COVERAGE_END
        )
        meta = self.metadata()
        resolution_m = self._resolution_m(meta)
        dims_expr = f"[({_iso_z(clamped_start)}):({_iso_z(clamped_end)})][({lat})][({lon})]"
        url = _csv_url(DATASET_OCEANSAT, ["CHL"], dims_expr)
        key = self._series_key(lat, lon, clamped_start, clamped_end)
        raw = self.get(url, key=key)
        note = (
            "INCOIS / ISRO Oceansat-2 OCM chlorophyll-a (incois_oceansat2_datasets) -- "
            "closed archive, 2011-02-02 to 2020-05-01, never current. Multi-year "
            "productivity diagnostic input only."
        )
        if was_clamped:
            note += " Requested time range clamped to the dataset's own coverage."
        return self._rows_to_observations(
            raw,
            requested_lat=lat,
            requested_lon=lon,
            resolution_m=resolution_m,
            time_range_clamped=was_clamped,
            note=note,
        )

    # -- generic Source contract --------------------------------------------------------

    def parse(self, raw: FetchResult, **kw: Any) -> list[Observation]:
        """Parses a ``.csv`` point/series ``FetchResult``. A ``.das``/``.dds`` metadata
        fetch carries no data values and parses to an empty list -- a valid outcome."""
        if not raw.url.endswith(".csv") and ".csv?" not in raw.url:
            return []
        lat0, lon0 = self.region.centre
        meta = self.metadata()
        resolution_m = self._resolution_m(meta)
        note = (
            "INCOIS / ISRO Oceansat-2 OCM chlorophyll-a (incois_oceansat2_datasets), "
            "closed archive -- never current."
        )
        return self._rows_to_observations(
            raw,
            requested_lat=lat0,
            requested_lon=lon0,
            resolution_m=resolution_m,
            time_range_clamped=False,
            note=note,
        )

    def fetch(self, **kwargs: Any) -> FetchResult:
        return self._das_fetch()

    def health(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            meta = self.metadata()
            resolution_m = self._resolution_m(meta)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            coverage_end = _parse_erddap_time(meta.get("time_coverage_end"))
            return {
                "source_id": self.source_id,
                "ok": True,
                "count": len(meta.get("variables", {})),
                "latency_ms": latency_ms,
                "issued_at": (
                    coverage_end.isoformat() if coverage_end else meta.get("time_coverage_end")
                ),
                #: Always "expired" by design -- see the class docstring. Never derived
                #: to look otherwise, however far in the past ``utcnow()`` sits.
                "freshness": "expired",
                "resolution_m": resolution_m,
                "error": None,
                "time_coverage_start": meta.get("time_coverage_start"),
                "time_coverage_end": meta.get("time_coverage_end"),
                "grid": {
                    "lat_range": meta.get("lat_range"),
                    "lon_range": meta.get("lon_range"),
                },
                "archive_closed": True,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "source_id": self.source_id,
                "ok": False,
                "count": 0,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "issued_at": None,
                "freshness": None,
                "resolution_m": self.spatial_resolution_m,
                "error": f"{type(exc).__name__}: {exc}",
            }


__all__ = [
    "ERDDAP", "DATASET", "IncoisArgo",
    "DATASET_OCEANSAT", "OCEANSAT_COVERAGE_START", "OCEANSAT_COVERAGE_END",
    "OCEANSAT_FALLBACK_RESOLUTION_M", "IncoisOceansat",
]
