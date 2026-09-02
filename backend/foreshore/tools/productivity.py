"""Tool 13 -- "why has fish productivity declined in this region?"

The differentiator: a diagnostic no competing team's canned advisory chatbot attempts,
because it requires reasoning over two genuinely different time depths honestly.

**The honesty constraint this module exists to enforce.** Two real INCOIS sources feed
this diagnostic, and they must never be blurred together:

* :class:`~foreshore.sources.incois_erddap.IncoisArgo` (``incois_argo_10d_VAM``) really
  does span a long history -- its own ``MAX_TIMESERIES_SPAN_DAYS`` bounds one query to
  ~9 years, and the live dataset's ``time_coverage_start`` is 2004-01-10 (see that
  module's docstring). This is the one genuinely multi-year signal here: subsurface
  temperature warming/cooling at depth.
* :class:`~foreshore.sources.incois_thredds.IncoisThredds` ``chl``/``sst`` products are
  **short rolling windows** -- CLAUDE.md documents chlorophyll as a 3-day rolling
  composite and the OSF nest generally as a forward-looking ~7-day forecast product, not
  an archive. ``catalog_dates(product)`` is asked, live, exactly how many dates the
  server currently holds, and whatever comes back -- one date, three, however many -- is
  what gets reported. This module never states a chlorophyll/SST time span it did not
  actually retrieve, and never narrates a "recent trend" as if it were a multi-year
  climate record.

Every number this tool emits is a real :class:`~foreshore.models.Observation` with its
own :class:`~foreshore.models.Provenance`, following ``pfz_derived.py``'s pattern: the
*statistics* (a linear-trend slope, a recent-window delta) are FORESHORE's own derived
diagnostic over raw retrieved series -- ``emits_derived=True``, every derived
observation's ``Provenance.is_derived`` is ``True``, and the summary opens by naming
this as FORESHORE's own diagnostic, never the official INCOIS advisory.

**Caching.** The Argo trend is expensive (a slow ERDDAP round-trip over a bounded but
real time series) and, per the plan, does not meaningfully change day to day -- so it is
computed once and cached via ``store/cache.py``'s existing generic snapshot mechanism
under a dedicated ``productivity_trends`` bucket, reused for 30 days. The chlorophyll/
SST recent-window read is cheap and is itself a live rolling window that changes daily,
so it is recomputed fresh on every call and never cached.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from ..config import load_region
from ..models import UTC, Observation, Provenance, ToolResult, utcnow
from ..store.cache import read_latest_cache, write_snapshot
from .registry import registry

#: A computed Argo slope smaller than this (either sign) is reported as "stable" rather
#: than warming/cooling -- deliberately small relative to the order-0.1-0.3 degC/decade
#: multi-decadal ocean warming signals in the literature, so a noise-level slope from a
#: short or gappy real series is not over-narrated as a trend.
ARGO_STABLE_EPSILON_C_PER_DECADE = 0.05

#: The Argo trend is a precomputed, cached diagnostic -- 30 days, per the plan: "the
#: data is multi-year and does not change [day to day] ... that is honest and cheap."
ARGO_TREND_CACHE_MAX_AGE_S = 30 * 86400.0

#: ``store/cache.py`` bucket name for the cached Argo trend computation. Distinct from
#: any real source's ``source_id`` -- this is FORESHORE's own derived-statistic cache,
#: not a source snapshot.
ARGO_TREND_CACHE_SOURCE = "productivity_trends"

_DEFAULT_REQUESTED_DEPTH_M = 5.0
_DEFAULT_YEARS = 10

#: real spatial resolutions of the underlying grids, used on the derived Provenance
#: records below -- matches IncoisArgo.spatial_resolution_m / the incois_thredds.py
#: PRODUCTS table, not invented constants.
_ARGO_RESOLUTION_M = 111_000.0
_PRODUCT_RESOLUTION_M = {"chl": 4_000.0, "sst": 9_260.0}


def _bbox_centroid(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    minlon, minlat, maxlon, maxlat = bbox
    return ((minlat + maxlat) / 2.0, (minlon + maxlon) / 2.0)


def _pick_depth_m(depth_range: tuple[float | None, float | None]) -> float:
    """Shallowest sensible standard depth, clamped into what the source's own
    ``metadata()`` actually reports -- never a value that might fall outside it."""
    depth_min, depth_max = depth_range
    if depth_min is None or depth_max is None:
        return _DEFAULT_REQUESTED_DEPTH_M
    lo, hi = (depth_min, depth_max) if depth_min <= depth_max else (depth_max, depth_min)
    return max(lo, min(_DEFAULT_REQUESTED_DEPTH_M, hi))


def _cache_key(lat: float, lon: float, depth_m: float, years: int) -> str:
    blob = f"{lat:.3f}:{lon:.3f}:{depth_m:.1f}:{years}"
    return f"argo:{hashlib.sha1(blob.encode()).hexdigest()[:16]}"


def _linear_trend_c_per_decade(times: list[datetime], values: list[float]) -> float | None:
    """``numpy.polyfit`` degree-1 slope, converted from degC/day to degC/decade. ``None``
    -- never a fabricated 0.0 -- when there are fewer than two distinct real timestamps
    to fit a line through."""
    if len(times) < 2:
        return None
    t0 = min(times)
    xs = np.array([(t - t0).total_seconds() / 86400.0 for t in times], dtype=float)
    ys = np.array(values, dtype=float)
    if np.allclose(xs, xs[0]):
        return None
    slope_per_day, _intercept = np.polyfit(xs, ys, 1)
    return float(slope_per_day) * 365.25 * 10.0


def _direction_label(slope_c_per_decade: float | None) -> str:
    if slope_c_per_decade is None:
        return "insufficient_data"
    if slope_c_per_decade > ARGO_STABLE_EPSILON_C_PER_DECADE:
        return "warming"
    if slope_c_per_decade < -ARGO_STABLE_EPSILON_C_PER_DECADE:
        return "cooling"
    return "stable"


# ----------------------------------------------------------------------------------
# Signal 1: Argo subsurface trend -- genuinely multi-year, cached.
# ----------------------------------------------------------------------------------


def _compute_argo_trend(region: Any, lat: float, lon: float, years: int) -> tuple[dict[str, Any] | None, str | None]:
    """Returns ``(trend_dict, None)`` on success or ``(None, reason)`` on a stated gap.
    Never raises -- a missing/broken Argo adapter, an empty series, or a genuinely
    single-point series are all abstentions, not crashes."""
    try:
        from ..sources.incois_erddap import MAX_TIMESERIES_SPAN_DAYS, IncoisArgo
    except Exception as exc:  # noqa: BLE001
        return None, f"incois_erddap adapter unavailable: {type(exc).__name__}: {exc}"

    argo = IncoisArgo(region=region)

    depth_m = _DEFAULT_REQUESTED_DEPTH_M
    try:
        meta = argo.metadata()
        depth_m = _pick_depth_m(tuple(meta.get("depth_range", (None, None))))
    except Exception:  # noqa: BLE001 -- metadata is a nicety for depth choice, not fatal
        pass

    cache_key = _cache_key(lat, lon, depth_m, years)
    cached = read_latest_cache(ARGO_TREND_CACHE_SOURCE, cache_key, ARGO_TREND_CACHE_MAX_AGE_S)
    if cached is not None and isinstance(cached.payload, dict) and cached.payload.get("status") == "ok":
        return cached.payload, None

    now = utcnow()
    requested_days = 365.25 * years
    start = now - timedelta(days=requested_days)
    try:
        obs = argo.timeseries(lat, lon, depth_m, start, now)
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"

    temp_obs = sorted(
        (o for o in obs if o.variable == "subsurface_temperature" and o.is_numeric),
        key=lambda o: o.valid_time,
    )
    if not temp_obs:
        return None, "no subsurface_temperature observations returned for this point/depth/span"

    times = [o.valid_time for o in temp_obs]
    values = [float(o.value) for o in temp_obs]
    slope = _linear_trend_c_per_decade(times, values)
    obs_start, obs_end = times[0], times[-1]

    result: dict[str, Any] = {
        "status": "ok",
        "depth_m": depth_m,
        "requested_years": years,
        "clamped_by_source": requested_days > MAX_TIMESERIES_SPAN_DAYS,
        "n_points": len(temp_obs),
        "slope_c_per_decade": slope,
        "direction": _direction_label(slope),
        "mean_temp_degc": float(np.mean(values)),
        "obs_start": obs_start.isoformat(),
        "obs_end": obs_end.isoformat(),
        "actual_span_days": (obs_end - obs_start).total_seconds() / 86400.0,
        "grid_lat": temp_obs[0].qualifiers.get("grid_lat"),
        "grid_lon": temp_obs[0].qualifiers.get("grid_lon"),
        "source_url": temp_obs[-1].provenance.url,
        "series": [{"t": t.isoformat(), "v": v} for t, v in zip(times, values)],
    }
    write_snapshot(
        ARGO_TREND_CACHE_SOURCE, cache_key, result["source_url"], result,
        {"depth_m": depth_m, "years": years, "lat": lat, "lon": lon},
    )
    return result, None


# ----------------------------------------------------------------------------------
# Signals 2/3: chlorophyll + SST recent rolling-window indicator -- never cached.
# ----------------------------------------------------------------------------------


def _compute_recent_window_trend(thredds: Any, product: str, lat: float, lon: float) -> tuple[dict[str, Any] | None, str | None]:
    """Reads *every real date* ``catalog_dates(product)`` actually reports right now --
    could be one, could be several -- and computes a first-vs-last delta (>=2 points) or
    a linear fit (>=3 points) over exactly that real span. Never fabricates a trend from
    a single point; that case is reported back for the caller to label as a single
    reading, not a trend."""
    try:
        from ..sources.incois_thredds import PRODUCT_CANONICAL_VARS
        from ..sources.incois_thredds import UNITS as THREDDS_UNITS
    except Exception as exc:  # noqa: BLE001
        return None, f"incois_thredds adapter unavailable: {type(exc).__name__}: {exc}"

    try:
        dates = sorted(thredds.catalog_dates(product))
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    if not dates:
        return None, f"no catalogue dates discovered for {product!r}"

    canonical_var = PRODUCT_CANONICAL_VARS[product][0]
    points: list[tuple[Any, float, Observation]] = []
    last_error: str | None = None
    for d in dates:
        at = datetime(d.year, d.month, d.day, tzinfo=UTC)
        try:
            obs_list = thredds.point(product, lat, lon, at=at)
        except Exception as exc:  # noqa: BLE001 -- one bad date must not sink the rest
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        match = next((o for o in obs_list if o.variable == canonical_var and o.is_numeric), None)
        if match is not None:
            points.append((d, float(match.value), match))

    if not points:
        reason = last_error or f"no valid (non-fill) {canonical_var} value at this point on any cataloged date"
        return None, reason

    points.sort(key=lambda p: p[0])
    dates_used = [p[0] for p in points]
    values = [p[1] for p in points]
    sample_obs = [p[2] for p in points]

    if len(points) == 1:
        method, delta = "single_point", None
    elif len(points) == 2:
        method, delta = "first_vs_last", values[-1] - values[0]
    else:
        t0 = dates_used[0]
        xs = np.array([(d - t0).days for d in dates_used], dtype=float)
        ys = np.array(values, dtype=float)
        if np.allclose(xs, xs[0]):
            method, delta = "first_vs_last", values[-1] - values[0]
        else:
            slope_per_day, _b = np.polyfit(xs, ys, 1)
            method = "linear_fit"
            delta = float(slope_per_day) * float(xs[-1] - xs[0])

    return {
        "status": "ok",
        "product": product,
        "canonical_var": canonical_var,
        "unit": THREDDS_UNITS[canonical_var],
        "n_points": len(points),
        "method": method,
        "delta": delta,
        "first_value": values[0],
        "last_value": values[-1],
        "date_start": dates_used[0].isoformat(),
        "date_end": dates_used[-1].isoformat(),
        "span_days": (dates_used[-1] - dates_used[0]).days,
        "series": [{"date": d.isoformat(), "v": v} for d, v in zip(dates_used, values)],
        "provenance_url": sample_obs[-1].provenance.url,
        "provenance_acquired_at": sample_obs[-1].provenance.acquired_at.isoformat(),
    }, None


@registry.tool(
    name="get_productivity_history",
    number=13,
    description=(
        "FORESHORE's own diagnostic for 'why has fish productivity declined here': a "
        "multi-year INCOIS Argo subsurface temperature/salinity trend (genuinely up to "
        "~9 years, from incois_argo_10d_VAM, cached -- this signal does not change day "
        "to day) plus a short recent chlorophyll and sea-surface-temperature trend "
        "indicator from the INCOIS OSF rolling-composite feeds (genuinely only as long "
        "as the live catalogue actually holds -- typically a handful of days, never "
        "presented as a multi-year record). Every number traces to a retrieved "
        "observation with its own provenance; this is a FORESHORE derivation, never the "
        "official INCOIS advisory."
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
                    "Defaults to the active region's bbox; the Argo/OSF point queries "
                    "use its centroid (Argo and OSF point()/timeseries() take a point, "
                    "not a bbox)."
                ),
            },
            "years": {
                "type": ["integer", "null"],
                "description": (
                    "Requested span, in years, for the Argo subsurface trend. The "
                    "underlying source bounds a single query to ~9 years "
                    "(incois_argo_10d_VAM's own MAX_TIMESERIES_SPAN_DAYS); a larger "
                    "request is clamped server-side and the response says so "
                    "explicitly rather than silently. Default 10."
                ),
                "minimum": 1,
                "maximum": 30,
            },
        },
        "required": [],
    },
    specialists=("OceanAnalytics",),
    reads_sources=("incois_argo", "incois_osf_chl", "incois_osf_sst"),
    emits_derived=True,
    cost="slow",
)
def get_productivity_history(bbox: list[float] | None = None, years: int | None = None) -> ToolResult:
    """FORESHORE's productivity-decline diagnostic. Never raises: every adapter failure,
    empty series or single-point series degrades to a named entry in ``missing`` and an
    honest note, never a fabricated trend."""
    region = load_region()
    bbox_use = tuple(float(v) for v in bbox) if bbox else region.bbox
    years_use = int(years) if years else _DEFAULT_YEARS
    lat, lon = _bbox_centroid(bbox_use)

    observations: list[Observation] = []
    missing: list[str] = []
    driver_notes: list[str] = []
    diagnostics: dict[str, str] = {}
    series_payload: dict[str, Any] = {}

    # -- signal 1: Argo subsurface trend --------------------------------------------
    argo_trend, argo_err = _compute_argo_trend(region, lat, lon, years_use)
    if argo_trend is None:
        missing.append("argo_subsurface_trend")
        if argo_err:
            diagnostics["argo_subsurface_trend"] = argo_err
    else:
        series_payload["argo_temperature"] = argo_trend["series"]
        obs_start = datetime.fromisoformat(argo_trend["obs_start"])
        obs_end = datetime.fromisoformat(argo_trend["obs_end"])
        notes_bits = [
            f"linear trend (numpy.polyfit, degree 1) over {argo_trend['n_points']} INCOIS "
            f"gridded Argo 10-day objective analysis (incois_argo_10d_VAM) observations at "
            f"{argo_trend['depth_m']:.0f} m depth, actual retrieved span "
            f"{argo_trend['obs_start']} to {argo_trend['obs_end']} "
            f"({argo_trend['actual_span_days']:.0f} real days)",
        ]
        if argo_trend.get("clamped_by_source"):
            notes_bits.append(
                f"the requested {years_use}-year span exceeds the source's own bound "
                "(incois_argo_10d_VAM.MAX_TIMESERIES_SPAN_DAYS, ~9 years) and was "
                "clamped server-side -- the dates above are what was actually returned"
            )
        notes_bits.append("FORESHORE's own derived diagnostic, not an official INCOIS product")
        argo_prov = Provenance(
            source_id="foreshore_productivity_argo_trend",
            source_name="FORESHORE derived Argo subsurface temperature trend (from incois_argo)",
            authority="derived",
            url=argo_trend["source_url"],
            acquired_at=utcnow(),
            issued_at=obs_end,
            valid_from=obs_start,
            valid_to=obs_end,
            spatial_resolution_m=_ARGO_RESOLUTION_M,
            is_derived=True,
            notes="; ".join(notes_bits) + ".",
        )
        obs_lat = argo_trend.get("grid_lat") or lat
        obs_lon = argo_trend.get("grid_lon") or lon
        if argo_trend["slope_c_per_decade"] is not None:
            observations.append(Observation(
                variable="subsurface_temperature_trend",
                value=round(argo_trend["slope_c_per_decade"], 4),
                unit="degC/decade",
                lat=obs_lat, lon=obs_lon, valid_time=obs_end, provenance=argo_prov,
                qualifiers={
                    "direction": argo_trend["direction"],
                    "n_points": argo_trend["n_points"],
                    "depth_m": argo_trend["depth_m"],
                    "mean_temp_degc": round(argo_trend["mean_temp_degc"], 3),
                },
            ))
            driver_notes.append(
                f"subsurface temperature at {argo_trend['depth_m']:.0f} m is "
                f"{argo_trend['direction']} at {argo_trend['slope_c_per_decade']:+.3f} "
                f"degC/decade over {argo_trend['n_points']} Argo observations "
                f"({argo_trend['obs_start'][:10]} to {argo_trend['obs_end'][:10]})"
            )
        else:
            # A single real observation is not a trend -- report the reading, not a
            # fabricated slope, and still count the trend itself as missing.
            observations.append(Observation(
                variable="subsurface_temperature_single_reading",
                value=round(argo_trend["mean_temp_degc"], 3),
                unit="degC",
                lat=obs_lat, lon=obs_lon, valid_time=obs_end, provenance=argo_prov,
                qualifiers={
                    "n_points": argo_trend["n_points"],
                    "depth_m": argo_trend["depth_m"],
                    "note": "only one real Argo observation available at this point/depth/span -- insufficient for a trend",
                },
            ))
            missing.append("argo_subsurface_trend")

    # -- signals 2/3: chlorophyll + SST recent rolling-window indicators -----------
    thredds = None
    try:
        from ..sources.incois_thredds import IncoisThredds
        thredds = IncoisThredds(region=region)
    except Exception as exc:  # noqa: BLE001
        reason = f"incois_thredds adapter unavailable: {type(exc).__name__}: {exc}"
        diagnostics["chl_recent_trend"] = reason
        diagnostics["sst_recent_trend"] = reason
        missing.append("chl_recent_trend")
        missing.append("sst_recent_trend")

    if thredds is not None:
        for product, label in (("chl", "chlorophyll"), ("sst", "sea-surface temperature")):
            key = f"{product}_recent_trend"
            trend, err = _compute_recent_window_trend(thredds, product, lat, lon)
            if trend is None:
                missing.append(key)
                if err:
                    diagnostics[key] = err
                continue

            series_payload[f"{product}_recent"] = trend["series"]
            issued = datetime(*(int(x) for x in trend["date_end"].split("-")), tzinfo=UTC)
            valid_from = datetime(*(int(x) for x in trend["date_start"].split("-")), tzinfo=UTC)
            notes = (
                f"recent {trend['span_days']}-day rolling-window indicator from INCOIS "
                f"OSF '{product}' (incois_osf_{product}), {trend['n_points']} real "
                f"cataloged date(s) actually retrieved, {trend['date_start']} to "
                f"{trend['date_end']} -- NOT a multi-year record; the live catalogue for "
                f"this product currently holds only this many dates. FORESHORE's own "
                f"derived diagnostic, not an official INCOIS product."
            )
            prov = Provenance(
                source_id=f"foreshore_productivity_{product}_trend",
                source_name=f"FORESHORE derived recent {label} trend (from incois_osf_{product})",
                authority="derived",
                url=trend["provenance_url"],
                acquired_at=datetime.fromisoformat(trend["provenance_acquired_at"]),
                issued_at=issued,
                valid_from=valid_from,
                valid_to=issued,
                spatial_resolution_m=_PRODUCT_RESOLUTION_M[product],
                is_derived=True,
                notes=notes,
            )
            if trend["method"] == "single_point":
                observations.append(Observation(
                    variable=f"{trend['canonical_var']}_recent_level",
                    value=round(trend["last_value"], 4),
                    unit=trend["unit"],
                    lat=lat, lon=lon, valid_time=issued, provenance=prov,
                    qualifiers={
                        "n_points": 1,
                        "note": "only one real cataloged date available -- a single reading is reported, not a trend",
                    },
                ))
                missing.append(key)
                driver_notes.append(
                    f"{label} single recent reading ({trend['date_end']}): "
                    f"{trend['last_value']:.3f} {trend['unit']} -- insufficient real "
                    "dates for a trend"
                )
            else:
                observations.append(Observation(
                    variable=f"{trend['canonical_var']}_recent_delta",
                    value=round(trend["delta"], 4),
                    unit=trend["unit"],
                    lat=lat, lon=lon, valid_time=issued, provenance=prov,
                    qualifiers={
                        "n_points": trend["n_points"],
                        "method": trend["method"],
                        "first_value": trend["first_value"],
                        "last_value": trend["last_value"],
                    },
                ))
                driver_notes.append(
                    f"{label} moved {trend['delta']:+.3f} {trend['unit']} over the real "
                    f"{trend['span_days']}-day window actually retrieved "
                    f"({trend['date_start']} to {trend['date_end']}, {trend['n_points']} "
                    "cataloged date(s))"
                )

    payload: dict[str, Any] = {
        "bbox": list(bbox_use),
        "centroid": {"lat": lat, "lon": lon},
        "requested_years": years_use,
        "series": series_payload,
        "diagnostics": diagnostics,
        "disclaimer": (
            "FORESHORE's own diagnostic derivation over raw retrieved INCOIS series -- "
            "never the official INCOIS PFZ advisory or coastal bulletin."
        ),
    }

    if not observations:
        summary = (
            "FORESHORE productivity diagnostic -- insufficient data for a productivity "
            f"diagnostic right now (centred on {lat:.3f}, {lon:.3f}): "
            + "; ".join(f"{m} ({diagnostics.get(m, 'no data available')})" for m in missing)
            + ". Abstaining rather than inventing a causal narrative."
        )
        return ToolResult(
            tool="get_productivity_history", ok=True, partial=True, missing=missing,
            summary=summary, payload=payload,
        )

    summary_bits = [
        "FORESHORE productivity diagnostic (FORESHORE's own derivation, not an "
        f"official INCOIS product), centred on ({lat:.3f}, {lon:.3f}):"
    ]
    summary_bits.extend(f" {n}." for n in driver_notes)
    if missing:
        summary_bits.append(" Not available this run: " + ", ".join(sorted(set(missing))) + ".")
    summary = " ".join(summary_bits)

    return ToolResult(
        tool="get_productivity_history",
        ok=True,
        partial=bool(missing),
        missing=sorted(set(missing)),
        observations=observations,
        payload=payload,
        summary=summary,
    )


__all__ = ["get_productivity_history"]
