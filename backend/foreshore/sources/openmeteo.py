"""Open-Meteo cross-check adapter — marine wave/tide/current model + atmospheric forecast.

Two keyless APIs, both probed live against Rameswaram (9.2876, 79.3129) on 2026-08-30
before a single variable name was encoded here:

* ``marine-api.open-meteo.com/v1/marine`` — ECMWF-driven global wave model, ~28 km grid.
  Every variable in :data:`MARINE_VARS` came back fully populated (zero nulls across a
  72-hour probe window).
* ``api.open-meteo.com/v1/forecast`` — ECMWF IFS ``best_match`` atmospheric forecast.
  Every variable in :data:`FORECAST_VARS` came back fully populated over the same window.
  ``lightning_potential`` was probed alongside them and returned null for all 72 hours, as
  it does everywhere over India — confirming the CRITICAL RULE below. It is not in
  ``FORECAST_VARS`` and this module never requests or emits it.

Neither response reports its own spatial resolution, so this module states the two
figures the vendor documents for the underlying models: ~28 km for the global wave
model, ~11 km for ECMWF IFS (`best_match` at these coordinates resolves to IFS — no
finer regional model is offered here). If a future response ever carries its own
resolution metadata, prefer that over these constants; as of this probe it does not.

**This is the CROSS-CHECK source, not the authority.** Open-Meteo's global wave model is
far coarser than the INCOIS OSF 11 km assimilated coastal nest, and its atmospheric
forecast — even at IFS's native ~11 km — assimilates no Indian coastal observation network.
INCOIS and IMD govern; Open-Meteo exists so that disagreement between the two is shown
side by side, never averaged away. See :func:`three_source_note`, surfaced by the evidence
panel.

CRITICAL RULE — enforced here, not just documented: ``lightning_potential`` is null over
India. This module never requests it and never emits a variable with "lightning" in its
name. CAPE is emitted only as ``convective_available_potential_energy`` (J/kg), always
carrying qualifier ``not_a_lightning_probability=True``. The IMD nowcast
(``imd:NowcastWarningDistrict``) is the only lightning authority in this system — if it is
unavailable, callers abstain; they do not fall back to CAPE.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any, Sequence

from ..models import UTC, Observation, haversine_m, utcnow
from .base import Source

log = logging.getLogger("foreshore.sources.openmeteo")

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# raw open-meteo name -> (canonical variable name, unit emitted). Canonical names are
# consumed by the rest of the system — use them exactly, do not invent alternates.
MARINE_VARS: dict[str, tuple[str, str]] = {
    "wave_height": ("significant_wave_height", "m"),
    "wind_wave_height": ("wind_wave_height", "m"),
    "swell_wave_height": ("swell_wave_height", "m"),
    "wave_period": ("wave_period", "s"),
    "swell_wave_period": ("swell_wave_period", "s"),
    "wave_direction": ("wave_direction", "deg"),
    "sea_level_height_msl": ("sea_level_height", "m"),
    "ocean_current_velocity": ("current_speed", "km/h"),      # converted to m/s on emit
    "ocean_current_direction": ("current_direction", "deg"),
    "sea_surface_temperature": ("sea_surface_temperature", "degC"),
}
FORECAST_VARS: dict[str, tuple[str, str]] = {
    "wind_speed_10m": ("wind_speed", "km/h"),                 # converted to knots on emit
    "wind_gusts_10m": ("wind_gust", "km/h"),                  # converted to knots on emit
    "wind_direction_10m": ("wind_direction", "deg"),
    "precipitation": ("precipitation", "mm"),
    "cape": ("convective_available_potential_energy", "J/kg"),
    "visibility": ("visibility", "m"),
    "temperature_2m": ("air_temperature", "degC"),
    "relative_humidity_2m": ("relative_humidity", "%"),
    "pressure_msl": ("pressure_msl", "hPa"),
    "cloud_cover": ("cloud_cover", "%"),
}

#: Raw open-meteo names emitted in m/s instead of the vendor's native km/h.
_TO_MS = frozenset({"ocean_current_velocity"})
#: Raw open-meteo names emitted in knots instead of the vendor's native km/h — every
#: vessel threshold in this system is in knots.
_TO_KN = frozenset({"wind_speed_10m", "wind_gusts_10m"})

_KMH_TO_MS = 1.0 / 3.6
_KMH_TO_KN = 1.0 / 1.852


def _parse_hour(raw_time: str) -> datetime:
    """Open-Meteo hourly timestamps are naive local-looking strings; ``timezone=UTC`` is
    always requested, so they are interpreted as UTC, never as the server's local zone."""
    return datetime.fromisoformat(raw_time).replace(tzinfo=UTC)


def _window_for(when: datetime, now: datetime) -> tuple[int, int]:
    """(past_hours, forecast_hours) needed for a single Open-Meteo call to bracket ``when``.

    ``now`` must be a value the caller already captured, never a fresh internal
    ``utcnow()`` call here. ``.at()``'s "now" case (``when=None``) sets ``when = now``
    from the very same capture, so a request for "right now" always computes exactly
    ``delta_h = 0.0`` and always resolves to the same ``(past_hours, forecast_hours)``.
    A second, independent ``utcnow()`` call here would instead race the first: two calls
    microseconds apart straddle the real clock roughly 50/50, so the two nearly-identical
    "now" values sometimes disagree by a hair, ``delta_h`` flips sign, and this function
    returns a *different* window for what is logically the same request. In
    ``FORESHORE_MODE=fixture`` that difference changes the request params and therefore
    the fixture cache key (``store/cache.py::key_for``) — the exact bug this docstring
    exists to prevent from coming back: a demo asking "is it safe right now" would
    non-deterministically show Open-Meteo as present or missing from run to run, on a
    path whose whole point is to be immune to exactly this kind of flakiness.
    """
    delta_h = (when - now).total_seconds() / 3600.0
    if delta_h >= 0:
        return 0, max(1, math.ceil(delta_h) + 1)
    return max(1, math.ceil(-delta_h) + 1), 1


def _local_extrema(series: list[Observation]) -> list[dict[str, Any]]:
    """Local maxima/minima on an hourly-ordered series. Plateaus are treated as flat and
    do not themselves register — only a genuine change of slope direction does."""
    vals = [o.numeric for o in series]
    out: list[dict[str, Any]] = []
    prev_dir = 0
    for i in range(1, len(vals)):
        a, b = vals[i - 1], vals[i]
        if a is None or b is None:
            continue
        diff = b - a
        if diff == 0:
            continue
        cur_dir = 1 if diff > 0 else -1
        if prev_dir != 0 and cur_dir != prev_dir:
            kind = "high" if prev_dir > 0 else "low"
            out.append({
                "kind": kind,
                "time": series[i - 1].valid_time.isoformat(),
                "value": vals[i - 1],
                "unit": series[i - 1].unit,
            })
        prev_dir = cur_dir
    return out


class _OpenMeteoAdapter(Source):
    """Shared fetch/parse machinery for the two Open-Meteo endpoints. Not part of the
    public contract itself — :class:`OpenMeteoMarine` and :class:`OpenMeteoForecast` are."""

    url: str = ""
    var_map: dict[str, tuple[str, str]] = {}
    #: Vendor-side caps, probed live: marine forecast_days maxes at 10 (240 h); the
    #: general forecast endpoint maxes at 16 days (384 h).
    max_forecast_hours: int = 240
    max_past_hours: int = 240

    @property
    def _canon_to_raw(self) -> dict[str, str]:
        return {canon: raw for raw, (canon, _unit) in self.var_map.items()}

    def _resolve(self, lat: float | None, lon: float | None) -> tuple[float, float]:
        if lat is None or lon is None:
            port = self.region.anchor_ports[0]
            lat = port.lat if lat is None else lat
            lon = port.lon if lon is None else lon
        return float(lat), float(lon)

    def _raw_names(self, variables: Sequence[str] | None) -> list[str]:
        if not variables:
            return list(self.var_map.keys())
        canon_to_raw = self._canon_to_raw
        out: list[str] = []
        for v in variables:
            if v in self.var_map:
                out.append(v)
            elif v in canon_to_raw:
                out.append(canon_to_raw[v])
            else:
                log.warning("%s: unknown variable %r requested, skipping", self.source_id, v)
        return out

    def _call(
        self,
        lat: float,
        lon: float,
        variables: Sequence[str] | None,
        *,
        forecast_hours: int | None = None,
        past_hours: int | None = None,
    ):
        names = self._raw_names(variables)
        params: dict[str, Any] = {
            "latitude": lat,
            "longitude": lon,
            "hourly": ",".join(names) if names else ",".join(self.var_map.keys()),
            "timezone": "UTC",
            "models": "best_match",
        }
        if forecast_hours:
            params["forecast_hours"] = min(int(forecast_hours), self.max_forecast_hours)
        if past_hours:
            params["past_hours"] = min(int(past_hours), self.max_past_hours)
        return self.get(self.url, params=params, as_json=True)

    # -- contract ------------------------------------------------------------------

    def fetch(
        self,
        lat: float | None = None,
        lon: float | None = None,
        hours: int = 48,
        variables: Sequence[str] | None = None,
    ):
        lat, lon = self._resolve(lat, lon)
        return self._call(lat, lon, variables, forecast_hours=max(1, int(hours)))

    def parse(
        self,
        raw,
        lat: float | None = None,
        lon: float | None = None,
        variables: Sequence[str] | None = None,
    ) -> list[Observation]:
        payload = raw.payload if isinstance(raw.payload, dict) else {}
        hourly = payload.get("hourly") or {}
        units = payload.get("hourly_units") or {}
        times_raw = hourly.get("time") or []
        if not times_raw:
            return []

        grid_lat, grid_lon = payload.get("latitude"), payload.get("longitude")
        lat_eff = lat if lat is not None else grid_lat
        lon_eff = lon if lon is not None else grid_lon
        grid_distance_m = None
        if None not in (grid_lat, grid_lon, lat_eff, lon_eff):
            grid_distance_m = haversine_m(float(lat_eff), float(lon_eff), float(grid_lat), float(grid_lon))

        # The Open-Meteo response never names which underlying model served a value when
        # `models=best_match` is used (only explicit multi-model requests get suffixed
        # variable names); the response's own `model` field, when present, still wins.
        model = payload.get("model") or "best_match"

        note_bits = [
            f"Open-Meteo cross-check (models=best_match); grid cell ({grid_lat}, {grid_lon})"
        ]
        if grid_distance_m is not None:
            note_bits.append(f"{grid_distance_m:.0f} m from requested point")
        note_bits.append(
            "issued_at approximated as fetch time: this API does not expose the "
            "underlying NWP run timestamp"
        )

        prov = self.provenance(
            raw,
            issued_at=raw.acquired_at,
            spatial_resolution_m=self.spatial_resolution_m,
            notes="; ".join(note_bits),
        )

        times = [_parse_hour(t) for t in times_raw]
        names = self._raw_names(variables)
        out: list[Observation] = []
        for raw_name in names:
            series = hourly.get(raw_name)
            if series is None or raw_name not in self.var_map:
                continue
            canonical, unit = self.var_map[raw_name]
            raw_unit = units.get(raw_name, unit)
            for i, (t, v) in enumerate(zip(times, series)):
                if v is None:              # never fabricate a zero for a missing hour
                    continue
                value = float(v)
                out_unit = unit
                qualifiers: dict[str, Any] = {"model": model, "hourly_index": i}
                if raw_name in _TO_MS:
                    qualifiers["raw_value"] = value
                    qualifiers["raw_unit"] = raw_unit
                    value = value * _KMH_TO_MS
                    out_unit = "m/s"
                elif raw_name in _TO_KN:
                    qualifiers["raw_value"] = value
                    qualifiers["raw_unit"] = raw_unit
                    value = value * _KMH_TO_KN
                    out_unit = "kn"
                if raw_name == "cape":
                    qualifiers["not_a_lightning_probability"] = True
                if grid_distance_m is not None:
                    qualifiers["grid_distance_m"] = round(grid_distance_m, 1)
                out.append(self.observe(canonical, value, out_unit, lat_eff, lon_eff, t, prov, **qualifiers))
        return out

    def at(
        self,
        lat: float | None = None,
        lon: float | None = None,
        when: datetime | None = None,
        variables: Sequence[str] | None = None,
    ) -> list[Observation]:
        """Observations at the single hour nearest ``when`` (default: now)."""
        lat, lon = self._resolve(lat, lon)
        # One `now` capture, reused as both the "now" reference below and, when the
        # caller passed no `when`, as `when` itself — see _window_for's docstring for
        # why a second independent utcnow() call here would reintroduce the exact
        # fixture-mode nondeterminism this split-capture avoids.
        now = utcnow()
        when = when.replace(tzinfo=UTC) if (when and not when.tzinfo) else (when or now)
        past_h, fwd_h = _window_for(when, now=now)
        raw = self._call(lat, lon, variables, forecast_hours=fwd_h, past_hours=past_h)
        obs = self.parse(raw, lat=lat, lon=lon, variables=variables)
        if not obs:
            return []
        times = sorted({o.valid_time for o in obs})
        nearest = min(times, key=lambda t: abs((t - when).total_seconds()))
        return [o for o in obs if o.valid_time == nearest]

    def series(
        self,
        lat: float | None = None,
        lon: float | None = None,
        hours: int = 48,
        variables: Sequence[str] | None = None,
    ) -> list[Observation]:
        lat, lon = self._resolve(lat, lon)
        raw = self.fetch(lat, lon, hours=hours, variables=variables)
        return self.parse(raw, lat=lat, lon=lon, variables=variables)

    def health(self) -> dict[str, Any]:
        info = super().health()
        info["note"] = three_source_note()
        return info


class OpenMeteoMarine(_OpenMeteoAdapter):
    """Global ECMWF-driven wave/tide/current model. Cross-check only — INCOIS OSF governs."""

    source_id = "openmeteo_marine"
    source_name = "Open-Meteo Marine (ECMWF-driven global wave model)"
    authority = "ECMWF/Open-Meteo"
    spatial_resolution_m = 28000.0
    validity = timedelta(hours=6)
    cache_ttl_s = 900.0

    url = MARINE_URL
    var_map = MARINE_VARS
    max_forecast_hours = 240   # vendor caps forecast_days at 10
    max_past_hours = 240

    def tide_window(
        self,
        lat: float | None = None,
        lon: float | None = None,
        hours: int = 24,
    ) -> tuple[list[Observation], dict[str, Any]]:
        """``sea_level_height`` series plus ``{"next_high", "next_low", "extrema"}``,
        found as local maxima/minima on the series — never asserted, always computed."""
        lat, lon = self._resolve(lat, lon)
        series = self.series(lat, lon, hours=hours, variables=["sea_level_height"])
        series = sorted(series, key=lambda o: o.valid_time)
        extrema = _local_extrema(series)
        now = utcnow()
        next_high = next(
            (e for e in extrema if e["kind"] == "high" and datetime.fromisoformat(e["time"]) >= now),
            None,
        )
        next_low = next(
            (e for e in extrema if e["kind"] == "low" and datetime.fromisoformat(e["time"]) >= now),
            None,
        )
        payload = {"next_high": next_high, "next_low": next_low, "extrema": extrema}
        return series, payload


class OpenMeteoForecast(_OpenMeteoAdapter):
    """ECMWF IFS (``best_match``) atmospheric forecast. Cross-check only — the IMD
    Coastal Bulletin and IMD nowcast govern; this source never carries a lightning value."""

    source_id = "openmeteo_forecast"
    source_name = "Open-Meteo Forecast (ECMWF IFS / best_match)"
    authority = "ECMWF/Open-Meteo"
    spatial_resolution_m = 11000.0
    validity = timedelta(hours=6)
    cache_ttl_s = 900.0

    url = FORECAST_URL
    var_map = FORECAST_VARS
    max_forecast_hours = 384   # vendor caps forecast_days at 16
    max_past_hours = 384


def three_source_note() -> str:
    """One-line evidence-panel caption: what this source is for, and what it is not."""
    return (
        "Open-Meteo (ECMWF-driven, ~28 km global wave grid / ~11 km IFS atmosphere) is a "
        "coarse global cross-check shown to surface disagreement, not an authority: the "
        "INCOIS OSF coastal nest (~11 km, assimilated) and the IMD Coastal Bulletin govern "
        "the advisory ceiling."
    )


__all__ = [
    "MARINE_URL", "FORECAST_URL", "MARINE_VARS", "FORECAST_VARS",
    "OpenMeteoMarine", "OpenMeteoForecast", "three_source_note",
]
