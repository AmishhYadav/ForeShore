"""Tool 13 -- "why has fish productivity declined in this region?"

The differentiator: a diagnostic no competing team's canned advisory chatbot attempts,
because it requires reasoning over genuinely different time depths honestly.

**The honesty constraint this module exists to enforce, restated for the sources it now
actually reads.** Three real sources feed this diagnostic and they must never be blurred
together, averaged, or narrated past what they actually cover:

* :class:`~foreshore.sources.incois_erddap.IncoisArgo` (``incois_argo_10d_VAM``) --
  subsurface temperature/salinity at depth, ``time_coverage_start`` 2004-01-10, its own
  ``MAX_TIMESERIES_SPAN_DAYS`` bounds one query to ~9 years. Unchanged from this module's
  first version: still the one signal that was never in question.
* :class:`~foreshore.sources.incois_erddap.IncoisOceansat` (``incois_oceansat2_datasets``)
  -- ISRO's own Oceansat-2 Ocean Colour Monitor, served from INCOIS's own ERDDAP.
  **This replaces the old chlorophyll signal**, which read INCOIS's live ``osf/chl``
  product (:class:`~foreshore.sources.incois_thredds.IncoisThredds`) and was *always*
  empty here: that grid is a Pacific Islands Countries product, lon 129.98-215.02 E --
  Palk Bay has never once been inside it, so this module answered a decadal question
  with a single Argo number on every real run. Oceansat is a **closed archive**,
  2011-02-02 to 2020-05-01 -- a real multi-year record, but a historical one that ends
  in 2020 and will never grow. Every sentence this module writes about it says so
  plainly and never calls it "recent" or "current". Falls back to NOAA MODIS-Aqua
  (:class:`~foreshore.sources.oceancolour.OceanColour`, ``product="modis"``) only when
  Oceansat itself is unreachable -- the two are never blended, and which one actually
  answered is always named.
* :class:`~foreshore.sources.oceancolour.OceanColour` ``sst_series`` (NOAA OISST v2.1,
  ``ncdcOisst21Agg``) -- **this replaces the old SST signal**, which read INCOIS's
  ``osf/sst`` product, a forward-looking ~7-day *forecast* nest with no history to take
  a trend over -- structurally incapable of answering "why has it changed", not merely
  unavailable. OISST is a genuine daily record back to the early 1980s and carries its
  **own published anomaly** against its own 1971-2000 climatology, so this module
  reports NOAA's own anomaly rather than computing one against a baseline FORESHORE
  picked (CLAUDE.md: "do not average disagreeing sources" applies just as much to
  "do not invent your own reference period when a published one exists").

Every number this tool emits is a real :class:`~foreshore.models.Observation` with its
own :class:`~foreshore.models.Provenance`, following ``pfz_derived.py``'s pattern: the
*statistics* (a linear-trend slope, its own standard error) are FORESHORE's own derived
diagnostic over raw retrieved series -- ``emits_derived=True``, every derived
observation's ``Provenance.is_derived`` is ``True`` -- while the SST anomaly is NOAA's
own published field, carried through with ``is_derived=False``. The summary opens by
naming this as FORESHORE's own diagnostic, never the official INCOIS/NOAA/ISRO advisory,
and when two signals move in different directions it says so plainly rather than
averaging them into one number.

**Caching.** All three trends are expensive round-trips over genuinely static or
near-static multi-year archives -- none of them meaningfully changes day to day -- so
all three are computed once and cached via ``store/cache.py``'s existing generic
snapshot mechanism, under the same ``productivity_trends`` bucket the Argo trend has
always used, each under its own key (see ``_cache_key``/``_chl_trend_cache_key``/
``_sst_trend_cache_key``). Every key is a function of latitude/longitude alone (plus,
for Argo, the requested depth and span) -- never of "now" — for the same reason
``test_incois_thredds_key.py`` exists: a key derived from the clock can never match on
a later call, which silently and permanently drops a source from every cached lookup.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
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
#: short or gappy real series is not over-narrated as a trend. Reused, unchanged, for the
#: sea-surface temperature trend below: both are degC/decade quantities and there is no
#: reason a shallower/deeper layer of the same water column should be held to a
#: different noise standard.
ARGO_STABLE_EPSILON_C_PER_DECADE = 0.05

#: All three trends are static-or-near-static multi-year archives, cached the same way
#: for the same reason: the Argo trend originally documented this as "the data is
#: multi-year and does not change [day to day] ... that is honest and cheap," and that
#: reasoning holds identically for the Oceansat/MODIS chlorophyll archive and the OISST
#: SST record now sitting beside it.
TREND_CACHE_MAX_AGE_S = 30 * 86400.0

#: ``store/cache.py`` bucket shared by all three trend computations below. Distinct from
#: any real source's ``source_id`` -- this is FORESHORE's own derived-statistic cache,
#: not a source snapshot. Each signal gets its own key inside this one bucket (see the
#: ``_..._cache_key`` helpers) rather than its own bucket, matching the pattern the Argo
#: trend already established.
PRODUCTIVITY_TREND_CACHE_SOURCE = "productivity_trends"

_DEFAULT_REQUESTED_DEPTH_M = 5.0
_DEFAULT_YEARS = 10

#: real spatial resolution of the Argo grid, used on its derived Provenance record below
#: -- matches IncoisArgo.spatial_resolution_m, not an invented constant. The chlorophyll
#: and SST derived Provenance records below instead read their resolution live off the
#: retrieved Observations themselves (``Provenance.spatial_resolution_m``), since both
#: adapters already compute that from the grid's own declared attributes.
_ARGO_RESOLUTION_M = 111_000.0

#: Deliberately wider than either new archive really is, so this module never has to
#: keep an exact archive boundary in sync with ``incois_erddap.py``/``oceancolour.py``.
#: Both adapters clamp internally to their own real coverage (Oceansat to its closed
#: 2011-02-02..2020-05-01 window, OISST to its own record) and flag the clamp on every
#: returned Observation's ``qualifiers["time_range_clamped"]`` -- this module only has to
#: ask for "everything you have" and then report, honestly, what actually came back.
_WIDE_LOOKBACK_YEARS = 30.0

#: Ordinary-language names for the three signals this tool can report or abstain on.
#: Used to build the user-facing summary -- never splice an internal key like
#: ``"chl_recent_trend"`` into prose (constraint 3: no `_`-joined internal identifiers
#: in a tool's ``summary``).
_SIGNAL_WORDS: dict[str, str] = {
    "argo_subsurface_trend": "subsurface temperature",
    "chlorophyll_trend": "chlorophyll",
    "sst_trend": "sea-surface temperature",
}


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


def _chl_trend_cache_key(lat: float, lon: float) -> str:
    """Deterministic in lat/lon alone -- no wall-clock instant, no ``years`` (the
    chlorophyll trend always requests the full available archive; see
    ``_WIDE_LOOKBACK_YEARS``), no marker for which product ultimately answered (Oceansat
    vs. the MODIS fallback), so a fallback answer this run does not permanently shadow a
    real Oceansat answer once it becomes reachable again -- each cache write records
    which product it came from inside the payload, and the next successful write simply
    replaces it, exactly like the Argo trend's own single-key/latest-wins pattern."""
    blob = f"{lat:.3f}:{lon:.3f}"
    return f"chl:{hashlib.sha1(blob.encode()).hexdigest()[:16]}"


def _sst_trend_cache_key(lat: float, lon: float) -> str:
    """Deterministic in lat/lon alone -- see ``_chl_trend_cache_key``."""
    blob = f"{lat:.3f}:{lon:.3f}"
    return f"sst:{hashlib.sha1(blob.encode()).hexdigest()[:16]}"


@dataclass(frozen=True)
class _TrendFit:
    """Shared least-squares fit result. ``se_per_decade`` is only populated with >= 3
    points (one residual degree of freedom); at 2 points a line is exact and has no
    residual to estimate noise from, and below 2 there is no line at all."""

    slope_per_decade: float | None
    se_per_decade: float | None
    n: int


def _linear_trend_fit(times: list[datetime], values: list[float]) -> _TrendFit:
    """The one place slope-per-decade arithmetic lives in this module -- Argo subsurface
    temperature, chlorophyll and sea-surface temperature all go through this single
    routine rather than three separate copies of the same ``numpy.polyfit`` call.

    ``numpy.polyfit`` degree-1 slope, converted from <unit>/day to <unit>/decade (the
    caller's values are whatever unit they are -- degC, mg/m^3 -- this function is
    unit-agnostic). Also returns the fitted slope's own standard error, in the same
    per-decade unit, wherever there are enough points to estimate one: this is what lets
    a caller build a noise floor from the data's own scatter instead of an invented round
    number (see ``get_productivity_history``'s chlorophyll noise-floor comment).
    ``slope_per_decade`` is ``None`` -- never a fabricated 0.0 -- when there are fewer
    than two distinct real timestamps to fit a line through.
    """
    n = len(times)
    if n < 2:
        return _TrendFit(None, None, n)
    t0 = min(times)
    xs = np.array([(t - t0).total_seconds() / 86400.0 for t in times], dtype=float)
    ys = np.array(values, dtype=float)
    if np.allclose(xs, xs[0]):
        return _TrendFit(None, None, n)
    slope_per_day, intercept = np.polyfit(xs, ys, 1)
    slope_per_decade = float(slope_per_day) * 365.25 * 10.0
    se_per_decade: float | None = None
    if n >= 3:
        dof = n - 2
        sxx = float(np.sum((xs - xs.mean()) ** 2))
        if dof > 0 and sxx > 0:
            residuals = ys - (slope_per_day * xs + intercept)
            residual_var = float(np.sum(residuals ** 2) / dof)
            se_per_day = math.sqrt(residual_var / sxx)
            se_per_decade = se_per_day * 365.25 * 10.0
    return _TrendFit(slope_per_decade, se_per_decade, n)


def _direction_label(
    slope: float | None, epsilon: float, *, rising: str = "warming", falling: str = "cooling"
) -> str:
    """``slope`` beyond +/- ``epsilon`` is ``rising``/``falling``; inside it, "stable";
    ``None`` -- insufficient real data to fit any line -- is reported as such, never as
    "stable" (a slope that could not be computed is not evidence of no change)."""
    if slope is None:
        return "insufficient_data"
    if slope > epsilon:
        return rising
    if slope < -epsilon:
        return falling
    return "stable"


# ----------------------------------------------------------------------------------
# Signal 1: Argo subsurface trend -- genuinely multi-year, cached. Unchanged logic.
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
    cached = read_latest_cache(PRODUCTIVITY_TREND_CACHE_SOURCE, cache_key, TREND_CACHE_MAX_AGE_S)
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
    fit = _linear_trend_fit(times, values)
    slope = fit.slope_per_decade
    obs_start, obs_end = times[0], times[-1]

    result: dict[str, Any] = {
        "status": "ok",
        "depth_m": depth_m,
        "requested_years": years,
        "clamped_by_source": requested_days > MAX_TIMESERIES_SPAN_DAYS,
        "n_points": len(temp_obs),
        "slope_c_per_decade": slope,
        "direction": _direction_label(slope, ARGO_STABLE_EPSILON_C_PER_DECADE),
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
        PRODUCTIVITY_TREND_CACHE_SOURCE, cache_key, result["source_url"], result,
        {"depth_m": depth_m, "years": years, "lat": lat, "lon": lon},
    )
    return result, None


# ----------------------------------------------------------------------------------
# Signal 2: chlorophyll -- ISRO Oceansat-2 archive (2011-2020), NOAA MODIS fallback.
# ----------------------------------------------------------------------------------


def _fit_chlorophyll_series(obs: list[Observation]) -> dict[str, Any] | None:
    """Shapes a ``chlorophyll_a`` Observation series from either adapter (both
    :class:`IncoisOceansat` and :class:`OceanColour` already emit
    ``variable="chlorophyll_a"``, ``unit="mg/m^3"``) into the trend dict this module
    caches and reports. Returns ``None`` when every value at this point is missing
    (all-NaN), never a fabricated trend."""
    numeric = sorted(
        (o for o in obs if o.variable == "chlorophyll_a" and o.is_numeric),
        key=lambda o: o.valid_time,
    )
    if not numeric:
        return None
    times = [o.valid_time for o in numeric]
    values = [float(o.value) for o in numeric]
    fit = _linear_trend_fit(times, values)
    last = numeric[-1]
    return {
        "status": "ok",
        "n_points": len(numeric),
        "slope_mg_m3_per_decade": fit.slope_per_decade,
        "se_mg_m3_per_decade": fit.se_per_decade,
        "mean_mg_m3": float(np.mean(values)),
        "obs_start": times[0].isoformat(),
        "obs_end": times[-1].isoformat(),
        "time_range_clamped": any(bool(o.qualifiers.get("time_range_clamped")) for o in numeric),
        "source_id": last.provenance.source_id,
        "source_name": last.provenance.source_name,
        "authority": last.provenance.authority,
        "source_url": last.provenance.url,
        "spatial_resolution_m": last.provenance.spatial_resolution_m,
        "grid_lat": last.qualifiers.get("grid_lat", last.lat),
        "grid_lon": last.qualifiers.get("grid_lon", last.lon),
    }


def _compute_chl_trend(region: Any, lat: float, lon: float) -> tuple[dict[str, Any] | None, str | None]:
    """Chlorophyll multi-year trend. Prefers ISRO's own Oceansat-2 archive -- better
    provenance for a problem statement filed by ISRO/Department of Space than the NOAA
    fallback this codebase also carries, and it is the one product that actually covers
    this coast (INCOIS's own live ``osf/chl`` never has). Falls back to NOAA MODIS-Aqua
    only when Oceansat itself is unreachable; the two are never blended, and the reason
    each attempt failed is kept (in ordinary words, joined with ``; ``) for the caller to
    report if both fail. Never raises."""
    cache_key = _chl_trend_cache_key(lat, lon)
    cached = read_latest_cache(PRODUCTIVITY_TREND_CACHE_SOURCE, cache_key, TREND_CACHE_MAX_AGE_S)
    if cached is not None and isinstance(cached.payload, dict) and cached.payload.get("status") == "ok":
        return cached.payload, None

    now = utcnow()
    wide_start = now - timedelta(days=365.25 * _WIDE_LOOKBACK_YEARS)
    errors: list[str] = []
    trend: dict[str, Any] | None = None

    try:
        from ..sources.incois_erddap import IncoisOceansat
        oceansat = IncoisOceansat(region=region)
        obs = oceansat.chlorophyll_series(lat, lon, start=wide_start, end=now)
        trend = _fit_chlorophyll_series(obs)
        if trend is None:
            raise ValueError("no non-missing chlorophyll-a values at this point")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"the ISRO Oceansat-2 archive ({type(exc).__name__}: {exc})")

    if trend is None:
        try:
            from ..sources.oceancolour import OceanColour
            ocean_colour = OceanColour(region=region)
            obs = ocean_colour.chlorophyll_series(lat, lon, start=wide_start, end=now, product="modis")
            trend = _fit_chlorophyll_series(obs)
            if trend is None:
                raise ValueError("no non-missing chlorophyll-a values at this point")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"the NOAA MODIS fallback ({type(exc).__name__}: {exc})")
            return None, "; ".join(errors)

    write_snapshot(
        PRODUCTIVITY_TREND_CACHE_SOURCE, cache_key, trend["source_url"], trend, {"lat": lat, "lon": lon},
    )
    return trend, None


# ----------------------------------------------------------------------------------
# Signal 3: sea-surface temperature -- NOAA OISST v2.1, plus its own published anomaly.
# ----------------------------------------------------------------------------------


def _compute_sst_trend(region: Any, lat: float, lon: float) -> tuple[dict[str, Any] | None, str | None]:
    """Sea-surface temperature multi-year trend plus OISST's own published anomaly
    (``ncdcOisst21Agg`` via :class:`OceanColour`). The anomaly is the dataset's own
    field against its own climatology -- this module never derives one itself. Never
    raises."""
    cache_key = _sst_trend_cache_key(lat, lon)
    cached = read_latest_cache(PRODUCTIVITY_TREND_CACHE_SOURCE, cache_key, TREND_CACHE_MAX_AGE_S)
    if cached is not None and isinstance(cached.payload, dict) and cached.payload.get("status") == "ok":
        return cached.payload, None

    now = utcnow()
    wide_start = now - timedelta(days=365.25 * _WIDE_LOOKBACK_YEARS)
    try:
        from ..sources.oceancolour import OceanColour
        ocean_colour = OceanColour(region=region)
        obs = ocean_colour.sst_series(lat, lon, start=wide_start, end=now)
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"

    temp_obs = sorted(
        (o for o in obs if o.variable == "sea_surface_temperature" and o.is_numeric),
        key=lambda o: o.valid_time,
    )
    anomaly_obs = sorted(
        (o for o in obs if o.variable == "sea_surface_temperature_anomaly" and o.is_numeric),
        key=lambda o: o.valid_time,
    )
    if not temp_obs:
        return None, "no non-missing sea-surface temperature values at this point"

    times = [o.valid_time for o in temp_obs]
    values = [float(o.value) for o in temp_obs]
    fit = _linear_trend_fit(times, values)
    last = temp_obs[-1]
    last_anomaly = anomaly_obs[-1] if anomaly_obs else None

    result: dict[str, Any] = {
        "status": "ok",
        "n_points": len(temp_obs),
        "slope_c_per_decade": fit.slope_per_decade,
        "mean_temp_degc": float(np.mean(values)),
        "obs_start": times[0].isoformat(),
        "obs_end": times[-1].isoformat(),
        "time_range_clamped": any(bool(o.qualifiers.get("time_range_clamped")) for o in temp_obs),
        "source_id": last.provenance.source_id,
        "source_name": last.provenance.source_name,
        "authority": last.provenance.authority,
        "source_url": last.provenance.url,
        "spatial_resolution_m": last.provenance.spatial_resolution_m,
        "grid_lat": last.qualifiers.get("grid_lat", last.lat),
        "grid_lon": last.qualifiers.get("grid_lon", last.lon),
        "latest_anomaly_degc": (float(last_anomaly.value) if last_anomaly is not None else None),
        "latest_anomaly_time": (last_anomaly.valid_time.isoformat() if last_anomaly is not None else None),
    }
    write_snapshot(
        PRODUCTIVITY_TREND_CACHE_SOURCE, cache_key, result["source_url"], result, {"lat": lat, "lon": lon},
    )
    return result, None


@registry.tool(
    name="get_productivity_history",
    number=13,
    description=(
        "FORESHORE's own diagnostic for 'why has fish productivity declined here': a "
        "multi-year INCOIS Argo subsurface temperature trend (up to ~9 years, "
        "incois_argo_10d_VAM), a multi-year chlorophyll trend from ISRO's own "
        "Oceansat-2 Ocean Colour Monitor archive (2011-2020, falling back to NOAA "
        "MODIS-Aqua only if Oceansat is unreachable), and a multi-year sea-surface "
        "temperature trend plus NOAA's own published anomaly (OISST v2.1). Every number "
        "traces to a retrieved observation with its own provenance; this is a FORESHORE "
        "derivation, never the official INCOIS/ISRO/NOAA advisory, and disagreeing "
        "signals are always shown side by side, never averaged."
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
                    "Defaults to the active region's bbox; all three signals query its "
                    "centroid as a point."
                ),
            },
            "years": {
                "type": ["integer", "null"],
                "description": (
                    "Requested span, in years, for the Argo subsurface trend only. The "
                    "underlying source bounds a single query to ~9 years "
                    "(incois_argo_10d_VAM's own MAX_TIMESERIES_SPAN_DAYS); a larger "
                    "request is clamped server-side and the response says so "
                    "explicitly rather than silently. Default 10. The chlorophyll and "
                    "sea-surface temperature trends always request the full span each "
                    "source's own archive holds, independent of this parameter."
                ),
                "minimum": 1,
                "maximum": 30,
            },
        },
        "required": [],
    },
    specialists=("OceanAnalytics",),
    reads_sources=("incois_argo", "incois_oceansat2", "noaa_coastwatch"),
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
    argo_direction: str | None = None
    sst_direction: str | None = None

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
            argo_direction = argo_trend["direction"]
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
                    "obs_start": argo_trend["obs_start"],
                    "obs_end": argo_trend["obs_end"],
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

    # -- signal 2: chlorophyll trend (ISRO Oceansat-2, NOAA MODIS fallback) ---------
    chl_trend, chl_err = _compute_chl_trend(region, lat, lon)
    if chl_trend is None:
        missing.append("chlorophyll_trend")
        if chl_err:
            diagnostics["chlorophyll_trend"] = chl_err
    else:
        is_oceansat = chl_trend["source_id"] == "incois_oceansat2"
        product_phrase = (
            "the ISRO Oceansat-2 Ocean Colour Monitor archive (a closed historical "
            "record, 2011-02-02 to 2020-05-01 -- not current conditions)"
            if is_oceansat else
            "the NOAA MODIS-Aqua chlorophyll record (used as a fallback: the ISRO "
            "Oceansat-2 archive was unavailable this run)"
        )
        obs_start = datetime.fromisoformat(chl_trend["obs_start"])
        obs_end = datetime.fromisoformat(chl_trend["obs_end"])
        notes_bits = [
            f"linear trend (numpy.polyfit, degree 1) over {chl_trend['n_points']} real "
            f"chlorophyll-a observations from {product_phrase}, actual retrieved span "
            f"{chl_trend['obs_start']} to {chl_trend['obs_end']}",
        ]
        if chl_trend.get("time_range_clamped"):
            notes_bits.append(
                "the requested window exceeded this source's own archive coverage and "
                "was clamped server-side to it -- the dates above are what was "
                "actually returned"
            )
        notes_bits.append("FORESHORE's own derived diagnostic, not an official product")
        chl_prov = Provenance(
            source_id="foreshore_productivity_chl_trend",
            source_name=f"FORESHORE derived chlorophyll trend (from {chl_trend['source_name']})",
            authority="derived",
            url=chl_trend["source_url"],
            acquired_at=utcnow(),
            issued_at=obs_end,
            valid_from=obs_start,
            valid_to=obs_end,
            spatial_resolution_m=chl_trend["spatial_resolution_m"],
            is_derived=True,
            notes="; ".join(notes_bits) + ".",
        )
        slope = chl_trend["slope_mg_m3_per_decade"]
        se = chl_trend["se_mg_m3_per_decade"]
        product_short = "ISRO Oceansat-2 archive" if is_oceansat else "NOAA MODIS fallback"
        if slope is not None and se is not None:
            # The chlorophyll analogue of ARGO_STABLE_EPSILON_C_PER_DECADE cannot be a
            # fixed literature constant the way multi-decadal ocean warming can -- there
            # is no comparably canonical "background chlorophyll trend" figure to anchor
            # a round number to. Instead the noise floor is the fitted regression's own
            # standard error of the decade slope (residual variance over the
            # time-axis sum-of-squares, via ``_linear_trend_fit`` -- the same OLS
            # formula behind any textbook confidence interval on a fitted slope): "is
            # this slope distinguishable from zero given how noisy this specific
            # retrieved series actually is," derived from the data itself rather than
            # assumed. One standard error, not an arbitrary multiple of it, so the
            # floor is tied only to the series' own scatter, not to a chosen
            # confidence level.
            noise_floor = se
            direction = _direction_label(slope, noise_floor, rising="increasing", falling="declining")
            observations.append(Observation(
                variable="chlorophyll_a_trend",
                value=round(slope, 4),
                unit="mg/m^3/decade",
                lat=chl_trend["grid_lat"], lon=chl_trend["grid_lon"], valid_time=obs_end, provenance=chl_prov,
                qualifiers={
                    "direction": direction,
                    "n_points": chl_trend["n_points"],
                    "mean_mg_m3": round(chl_trend["mean_mg_m3"], 4),
                    "noise_floor_mg_m3_per_decade": round(noise_floor, 4),
                    "source": product_short,
                    "obs_start": chl_trend["obs_start"],
                    "obs_end": chl_trend["obs_end"],
                },
            ))
            driver_notes.append(
                f"chlorophyll ({product_short}) is {direction} at {slope:+.4f} "
                f"mg/m^3 per decade over {chl_trend['n_points']} real observations "
                f"({chl_trend['obs_start'][:10]} to {chl_trend['obs_end'][:10]})"
            )
        else:
            # Fewer than 3 real points: no residual degree of freedom to estimate a
            # noise floor from, so this module reports the level actually retrieved
            # rather than a slope it cannot defend as more than noise.
            observations.append(Observation(
                variable="chlorophyll_a_level",
                value=round(chl_trend["mean_mg_m3"], 4),
                unit="mg/m^3",
                lat=chl_trend["grid_lat"], lon=chl_trend["grid_lon"], valid_time=obs_end, provenance=chl_prov,
                qualifiers={
                    "n_points": chl_trend["n_points"],
                    "obs_start": chl_trend["obs_start"],
                    "obs_end": chl_trend["obs_end"],
                    "note": "too few real observations at this point to fit a defensible decade trend",
                },
            ))
            missing.append("chlorophyll_trend")
            driver_notes.append(
                f"chlorophyll ({product_short}): only {chl_trend['n_points']} real "
                "observation(s) at this point -- insufficient for a trend"
            )

    # -- signal 3: sea-surface temperature trend + NOAA's own anomaly ---------------
    sst_trend, sst_err = _compute_sst_trend(region, lat, lon)
    if sst_trend is None:
        missing.append("sst_trend")
        if sst_err:
            diagnostics["sst_trend"] = sst_err
    else:
        obs_start = datetime.fromisoformat(sst_trend["obs_start"])
        obs_end = datetime.fromisoformat(sst_trend["obs_end"])
        notes_bits = [
            f"linear trend (numpy.polyfit, degree 1) over {sst_trend['n_points']} real "
            f"daily NOAA OISST v2.1 (ncdcOisst21Agg) sea-surface temperature "
            f"observations, actual retrieved span {sst_trend['obs_start']} to "
            f"{sst_trend['obs_end']}",
        ]
        if sst_trend.get("time_range_clamped"):
            notes_bits.append(
                "the requested window exceeded this source's own record and was "
                "clamped server-side to it -- the dates above are what was actually "
                "returned"
            )
        notes_bits.append("FORESHORE's own derived diagnostic, not an official NOAA product")
        sst_prov = Provenance(
            source_id="foreshore_productivity_sst_trend",
            source_name=f"FORESHORE derived sea-surface temperature trend (from {sst_trend['source_name']})",
            authority="derived",
            url=sst_trend["source_url"],
            acquired_at=utcnow(),
            issued_at=obs_end,
            valid_from=obs_start,
            valid_to=obs_end,
            spatial_resolution_m=sst_trend["spatial_resolution_m"],
            is_derived=True,
            notes="; ".join(notes_bits) + ".",
        )
        slope = sst_trend["slope_c_per_decade"]
        if slope is not None:
            sst_direction = _direction_label(slope, ARGO_STABLE_EPSILON_C_PER_DECADE)
            observations.append(Observation(
                variable="sea_surface_temperature_trend",
                value=round(slope, 4),
                unit="degC/decade",
                lat=sst_trend["grid_lat"], lon=sst_trend["grid_lon"], valid_time=obs_end, provenance=sst_prov,
                qualifiers={
                    "direction": sst_direction,
                    "n_points": sst_trend["n_points"],
                    "mean_temp_degc": round(sst_trend["mean_temp_degc"], 3),
                    "obs_start": sst_trend["obs_start"],
                    "obs_end": sst_trend["obs_end"],
                },
            ))
            driver_notes.append(
                f"sea-surface temperature (NOAA OISST v2.1) is {sst_direction} at "
                f"{slope:+.3f} degC/decade over {sst_trend['n_points']} real daily "
                f"observations ({sst_trend['obs_start'][:10]} to {sst_trend['obs_end'][:10]})"
            )
        else:
            observations.append(Observation(
                variable="sea_surface_temperature_single_reading",
                value=round(sst_trend["mean_temp_degc"], 3),
                unit="degC",
                lat=sst_trend["grid_lat"], lon=sst_trend["grid_lon"], valid_time=obs_end, provenance=sst_prov,
                qualifiers={
                    "n_points": sst_trend["n_points"],
                    "note": "only one real OISST observation available at this point -- insufficient for a trend",
                },
            ))
            missing.append("sst_trend")

        # NOAA's own published anomaly -- not a FORESHORE derivation, so is_derived=False
        # and the Provenance names OISST directly rather than "FORESHORE derived ...".
        if sst_trend.get("latest_anomaly_degc") is not None:
            anomaly_time = datetime.fromisoformat(sst_trend["latest_anomaly_time"])
            anomaly_prov = Provenance(
                source_id=sst_trend["source_id"],
                source_name=sst_trend["source_name"],
                authority=sst_trend["authority"],
                url=sst_trend["source_url"],
                acquired_at=utcnow(),
                issued_at=anomaly_time,
                valid_from=anomaly_time,
                valid_to=anomaly_time,
                spatial_resolution_m=sst_trend["spatial_resolution_m"],
                is_derived=False,
                notes=(
                    "NOAA OISST v2.1's own published anomaly against its own 1971-2000 "
                    "climatology -- FORESHORE does not compute this baseline itself."
                ),
            )
            observations.append(Observation(
                variable="sea_surface_temperature_anomaly",
                value=round(sst_trend["latest_anomaly_degc"], 3),
                unit="degC",
                lat=sst_trend["grid_lat"], lon=sst_trend["grid_lon"], valid_time=anomaly_time,
                provenance=anomaly_prov,
                qualifiers={"reference_climatology": "NOAA OISST v2.1 published 1971-2000 baseline"},
            ))
            driver_notes.append(
                f"NOAA's own published anomaly against its 1971-2000 baseline was "
                f"{sst_trend['latest_anomaly_degc']:+.3f} degC as of "
                f"{sst_trend['latest_anomaly_time'][:10]}"
            )

    # -- disagreement check: subsurface vs. surface temperature ----------------------
    # Never averaged into one number -- CLAUDE.md's "do not average disagreeing
    # sources" applies as much between depths of the same water column as it does
    # between two competing products of the same variable. Only compared when both
    # actually produced a directional trend (not "insufficient_data"/"stable" noise).
    if (
        argo_direction in ("warming", "cooling")
        and sst_direction in ("warming", "cooling")
        and argo_direction != sst_direction
    ):
        driver_notes.append(
            "subsurface and sea-surface temperature trends move in opposite "
            "directions here -- reported separately, not averaged, since they "
            "measure different depths of the same water column"
        )

    payload: dict[str, Any] = {
        "bbox": list(bbox_use),
        "centroid": {"lat": lat, "lon": lon},
        "requested_years": years_use,
        "series": series_payload,
        "diagnostics": diagnostics,
        "disclaimer": (
            "FORESHORE's own diagnostic derivation over raw retrieved INCOIS/ISRO/NOAA "
            "series -- never the official INCOIS PFZ advisory or coastal bulletin."
        ),
    }

    if not observations:
        missing_words = [_SIGNAL_WORDS.get(m, m) for m in sorted(set(missing))]
        summary = (
            "FORESHORE productivity diagnostic -- insufficient data for a productivity "
            f"diagnostic right now (centred on {lat:.3f}, {lon:.3f}): "
            + ", ".join(missing_words)
            + " are all unavailable this run. Abstaining rather than inventing a "
            "causal narrative."
        )
        return ToolResult(
            tool="get_productivity_history", ok=True, partial=True, missing=missing,
            summary=summary, payload=payload,
        )

    summary_bits = [
        "FORESHORE productivity diagnostic (FORESHORE's own derivation, not an "
        f"official INCOIS/ISRO/NOAA product), centred on ({lat:.3f}, {lon:.3f}):"
    ]
    summary_bits.extend(f" {n}." for n in driver_notes)
    if missing:
        missing_words = [_SIGNAL_WORDS.get(m, m) for m in sorted(set(missing))]
        summary_bits.append(" Not available this run: " + ", ".join(missing_words) + ".")
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
