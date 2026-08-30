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


def _csv_url(variables: Sequence[str], dims_expr: str) -> str:
    query = ",".join(f"{v}{dims_expr}" for v in variables)
    return f"{ERDDAP}/griddap/{DATASET}.csv?{quote(query, safe='')}"


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
        url = _csv_url(_DATA_VARS, dims_expr)
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
        url = _csv_url(_DATA_VARS, dims_expr)
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


__all__ = ["ERDDAP", "DATASET", "IncoisArgo"]
