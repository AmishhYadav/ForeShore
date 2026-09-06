"""New-signal tests for tool 13, ``get_productivity_history``
(backend/foreshore/tools/productivity.py), covering the chlorophyll (ISRO Oceansat-2,
NOAA MODIS fallback) and sea-surface temperature (NOAA OISST v2.1) trends that replaced
the old, permanently-empty ``IncoisThredds``-backed signals.

``backend/tests/test_productivity.py`` keeps the pre-existing Argo-trend/caching/
abstention coverage (retargeted where the old assertions encoded a now-obsolete
contract — see that file's module docstring). This file adds the cases the rewrite
brief calls for that did not exist anywhere yet: the three-signal success path, the
Oceansat -> MODIS fallback, both-chlorophyll-sources-down degradation, the data-derived
chlorophyll noise floor, honest span reporting in Observation qualifiers, the "never
call a 2011-2020 archive recent/current" guard, the unsourced-number audit
(``foreshore.agents.runtime.check_unsourced_numbers``, the system's own invariant-3
check), and cache-key determinism.

Every fake below monkeypatches the real adapter *methods*
(``IncoisArgo.metadata``/``.timeseries``, ``IncoisOceansat.chlorophyll_series``,
``OceanColour.chlorophyll_series``/``.sst_series``) directly, exactly as
``test_productivity.py`` already does for Argo — standing in for the network round-trip,
never for the ``Observation``/``Provenance`` contract, so every fake Observation still
carries a real, fully-populated ``Provenance``. ``store/cache.py``'s ``CACHE_DIR`` is
isolated to a fresh ``tmp_path`` in every test that calls ``get_productivity_history``,
since the repo's real ``data/cache/productivity_trends`` already holds live-probed
entries (Argo, and Oceansat chlorophyll) that would otherwise leak into these tests and
falsify what a "fresh" call actually exercises.
"""

from __future__ import annotations

import inspect
import re
from datetime import date, datetime, timedelta

import pytest

from foreshore.agents.runtime import check_unsourced_numbers
from foreshore.models import UTC, Observation, Provenance, utcnow
from foreshore.sources.incois_erddap import IncoisArgo, IncoisOceansat
from foreshore.sources.oceancolour import OceanColour
from foreshore.store import cache as cache_store
from foreshore.tools.productivity import (
    _chl_trend_cache_key,
    _sst_trend_cache_key,
    get_productivity_history,
)

# --------------------------------------------------------------------------------------
# Synthetic, realistic fake series.
# --------------------------------------------------------------------------------------

#: Argo: 20 points, a clear ~0.4 degC/decade warming signal -- comfortably clears
#: ARGO_STABLE_EPSILON_C_PER_DECADE (0.05) so the direction is unambiguous.
_ARGO_N_POINTS = 20
_ARGO_WARMING_C_PER_DECADE = 0.4

#: Chlorophyll: 10 real yearly points across the actual Oceansat archive window
#: (2011-02-02 .. 2020-05-01), a clear decline (~-0.25 mg/m^3/decade, verified against
#: its own fitted standard error before writing this module: se ~= 0.003, two orders of
#: magnitude smaller than the slope).
_CHL_DECLINE_SERIES: dict[date, float] = {
    date(2011, 6, 1): 0.60, date(2012, 6, 1): 0.58, date(2013, 6, 1): 0.55,
    date(2014, 6, 1): 0.53, date(2015, 6, 1): 0.50, date(2016, 6, 1): 0.48,
    date(2017, 6, 1): 0.45, date(2018, 6, 1): 0.43, date(2019, 6, 1): 0.40,
    date(2020, 4, 15): 0.38,
}

#: Chlorophyll: 10 monthly points zigzagging around a mean of 0.40 with essentially no
#: real trend -- verified before writing this module: fitted slope ~= -0.114
#: mg/m^3/decade against its own fitted standard error ~= 0.262, i.e. |slope| < SE, so
#: this must be reported "stable", not narrated as a decline.
_CHL_STABLE_SERIES: dict[date, float] = {
    date(2015, m, 1): v
    for m, v in zip(
        range(1, 11),
        [0.40, 0.42, 0.38, 0.42, 0.38, 0.42, 0.38, 0.42, 0.38, 0.40],
    )
}

#: SST: 12 yearly points, a clear warming signal (~0.45 degC/decade), plus a published
#: anomaly per step.
_SST_WARM_SERIES: dict[date, float] = {date(2003 + i, 6, 1): 28.0 + i * 0.05 for i in range(12)}
_SST_ANOMALY_SERIES: dict[date, float] = {d: round(0.30 + i * 0.05, 3) for i, d in enumerate(sorted(_SST_WARM_SERIES))}


def _fake_argo_metadata(self: IncoisArgo) -> dict:
    return {"depth_range": (0.0, 2000.0), "dataset_id": "incois_argo_10d_VAM"}


def _make_fake_argo_timeseries(n: int = _ARGO_N_POINTS, warming_c_per_decade: float = _ARGO_WARMING_C_PER_DECADE):
    def _fake(self: IncoisArgo, lat: float, lon: float, depth_m: float, start: datetime, end: datetime) -> list[Observation]:
        times = [start + (end - start) * (i / (n - 1)) for i in range(n)]
        base_temp = 28.0
        prov = Provenance(
            source_id="incois_argo",
            source_name="INCOIS gridded Argo 10-day objective analysis (incois_argo_10d_VAM)",
            authority="INCOIS",
            url="https://erddap.incois.gov.in/erddap/griddap/incois_argo_10d_VAM.csv?fake",
            acquired_at=utcnow(),
            issued_at=times[-1], valid_from=times[0], valid_to=times[-1] + timedelta(days=15),
            spatial_resolution_m=111_000.0,
        )
        out = []
        for t in times:
            elapsed_days = (t - start).total_seconds() / 86400.0
            value = base_temp + (warming_c_per_decade / 3652.5) * elapsed_days
            out.append(Observation(
                variable="subsurface_temperature", value=value, unit="degs",
                lat=lat, lon=lon, valid_time=t, provenance=prov,
                qualifiers={"grid_lat": lat, "grid_lon": lon, "depth_m": depth_m},
            ))
        return out

    return _fake


def _make_fake_oceansat_chlorophyll(values_by_date: dict[date, float], *, clamped: bool = False):
    def _fake(self: IncoisOceansat, lat: float, lon: float, *, start: datetime, end: datetime) -> list[Observation]:
        out = []
        for d in sorted(values_by_date):
            dt = datetime(d.year, d.month, d.day, tzinfo=UTC)
            if dt < start or dt > end:
                continue
            prov = Provenance(
                source_id="incois_oceansat2",
                source_name="INCOIS / ISRO Oceansat-2 Ocean Colour Monitor (incois_oceansat2_datasets)",
                authority="ISRO/NRSC",
                url="https://erddap.incois.gov.in/erddap/griddap/incois_oceansat2_datasets.csv?fake",
                acquired_at=utcnow(), issued_at=dt, valid_from=dt, valid_to=dt,
                spatial_resolution_m=4_320.0,
            )
            out.append(Observation(
                variable="chlorophyll_a", value=values_by_date[d], unit="mg/m^3",
                lat=lat, lon=lon, valid_time=dt, provenance=prov,
                qualifiers={
                    "grid_lat": lat, "grid_lon": lon,
                    "requested_lat": lat, "requested_lon": lon,
                    "time_range_clamped": clamped,
                },
            ))
        return out

    return _fake


def _fake_oceansat_raises(self: IncoisOceansat, lat: float, lon: float, *, start: datetime, end: datetime) -> list[Observation]:
    raise RuntimeError("simulated Oceansat outage")


def _make_fake_modis_chlorophyll(values_by_date: dict[date, float], *, clamped: bool = False):
    def _fake(self: OceanColour, lat: float, lon: float, *, start: datetime, end: datetime, product: str = "gapfilled") -> list[Observation]:
        out = []
        for d in sorted(values_by_date):
            dt = datetime(d.year, d.month, d.day, tzinfo=UTC)
            if dt < start or dt > end:
                continue
            prov = Provenance(
                source_id="noaa_coastwatch",
                source_name="NOAA CoastWatch ERDDAP (VIIRS gap-filled chlorophyll, MODIS-Aqua chlorophyll, OISST v2.1)",
                authority="NOAA",
                url="https://coastwatch.pfeg.noaa.gov/erddap/griddap/erdMH1chla1day_R2022NRT.csv?fake",
                acquired_at=utcnow(), issued_at=dt, valid_from=dt, valid_to=dt,
                spatial_resolution_m=4_638.0,
            )
            out.append(Observation(
                variable="chlorophyll_a", value=values_by_date[d], unit="mg/m^3",
                lat=lat, lon=lon, valid_time=dt, provenance=prov,
                qualifiers={
                    "grid_lat": lat, "grid_lon": lon,
                    "requested_lat": lat, "requested_lon": lon,
                    "time_range_clamped": clamped,
                },
            ))
        return out

    return _fake


def _fake_modis_raises(self: OceanColour, lat: float, lon: float, *, start: datetime, end: datetime, product: str = "gapfilled") -> list[Observation]:
    raise RuntimeError("simulated NOAA MODIS outage")


def _make_fake_sst(temp_by_date: dict[date, float], anomaly_by_date: dict[date, float] | None = None, *, clamped: bool = False):
    anomaly_by_date = anomaly_by_date or {}

    def _fake(self: OceanColour, lat: float, lon: float, *, start: datetime, end: datetime) -> list[Observation]:
        out = []
        for d in sorted(temp_by_date):
            dt = datetime(d.year, d.month, d.day, tzinfo=UTC)
            if dt < start or dt > end:
                continue
            prov = Provenance(
                source_id="noaa_coastwatch",
                source_name="NOAA CoastWatch ERDDAP (VIIRS gap-filled chlorophyll, MODIS-Aqua chlorophyll, OISST v2.1)",
                authority="NOAA",
                url="https://coastwatch.pfeg.noaa.gov/erddap/griddap/ncdcOisst21Agg.csv?fake",
                acquired_at=utcnow(), issued_at=dt, valid_from=dt, valid_to=dt,
                spatial_resolution_m=27_830.0,
            )
            qualifiers = {
                "grid_lat": lat, "grid_lon": lon,
                "requested_lat": lat, "requested_lon": lon,
                "time_range_clamped": clamped,
            }
            out.append(Observation(
                variable="sea_surface_temperature", value=temp_by_date[d], unit="degC",
                lat=lat, lon=lon, valid_time=dt, provenance=prov, qualifiers=dict(qualifiers),
            ))
            if d in anomaly_by_date:
                out.append(Observation(
                    variable="sea_surface_temperature_anomaly", value=anomaly_by_date[d], unit="degC",
                    lat=lat, lon=lon, valid_time=dt, provenance=prov, qualifiers=dict(qualifiers),
                ))
        return out

    return _fake


def _fake_sst_raises(self: OceanColour, lat: float, lon: float, *, start: datetime, end: datetime) -> list[Observation]:
    raise RuntimeError("simulated OISST outage")


def _patch_all_three(monkeypatch: pytest.MonkeyPatch, tmp_path, *, chl=None, sst=None) -> None:
    """Isolate the trend cache to ``tmp_path`` and install realistic Argo + (caller
    supplied or default declining/warming) chlorophyll/SST fakes for all three signals."""
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(IncoisArgo, "metadata", _fake_argo_metadata)
    monkeypatch.setattr(IncoisArgo, "timeseries", _make_fake_argo_timeseries())
    monkeypatch.setattr(IncoisOceansat, "chlorophyll_series", chl or _make_fake_oceansat_chlorophyll(_CHL_DECLINE_SERIES))
    monkeypatch.setattr(OceanColour, "sst_series", sst or _make_fake_sst(_SST_WARM_SERIES, _SST_ANOMALY_SERIES))


# --------------------------------------------------------------------------------------
# 1. All three signals present -> three trend Observations, all three named in the
#    summary, partial is False.
# --------------------------------------------------------------------------------------


def test_all_three_signals_present_and_reported(monkeypatch, tmp_path):
    _patch_all_three(monkeypatch, tmp_path)

    result = get_productivity_history()

    assert result.ok is True
    assert result.partial is False
    assert result.missing == []

    trend_vars = {o.variable for o in result.observations if o.variable.endswith("_trend")}
    assert trend_vars == {
        "subsurface_temperature_trend", "chlorophyll_a_trend", "sea_surface_temperature_trend",
    }
    # The published anomaly rides along as its own, non-derived Observation.
    anomaly = [o for o in result.observations if o.variable == "sea_surface_temperature_anomaly"]
    assert len(anomaly) == 1
    assert anomaly[0].provenance.is_derived is False

    summary_lower = result.summary.lower()
    assert "subsurface temperature" in summary_lower
    assert "chlorophyll" in summary_lower
    assert "sea-surface temperature" in summary_lower

    # Constraint 3: no dataset ids, internal identifiers or exception names leak into
    # the user-facing summary.
    for banned in ("incois_oceansat2_datasets", "ncdcOisst21Agg", "IncoisOceansat", "RuntimeError", "Traceback"):
        assert banned not in result.summary


# --------------------------------------------------------------------------------------
# 2. Oceansat unavailable -> the NOAA MODIS fallback is used for the chlorophyll trend.
# --------------------------------------------------------------------------------------


def test_oceansat_unavailable_falls_back_to_modis(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(IncoisArgo, "metadata", _fake_argo_metadata)
    monkeypatch.setattr(IncoisArgo, "timeseries", _make_fake_argo_timeseries())
    monkeypatch.setattr(IncoisOceansat, "chlorophyll_series", _fake_oceansat_raises)
    monkeypatch.setattr(OceanColour, "chlorophyll_series", _make_fake_modis_chlorophyll(_CHL_DECLINE_SERIES))
    monkeypatch.setattr(OceanColour, "sst_series", _make_fake_sst(_SST_WARM_SERIES, _SST_ANOMALY_SERIES))

    result = get_productivity_history()

    assert result.ok is True
    assert "chlorophyll_trend" not in result.missing
    chl_obs = [o for o in result.observations if o.variable == "chlorophyll_a_trend"]
    assert len(chl_obs) == 1
    prov = chl_obs[0].provenance
    assert prov.authority == "derived"
    assert "NOAA" in prov.source_name or "MODIS" in prov.source_name
    assert "ISRO" not in prov.source_name and "Oceansat" not in prov.source_name
    assert chl_obs[0].qualifiers.get("source") == "NOAA MODIS fallback"
    assert "fallback" in prov.notes.lower()


# --------------------------------------------------------------------------------------
# 3. Both chlorophyll sources unavailable -> partial=True, chlorophyll named in ordinary
#    words, the other two signals still reported.
# --------------------------------------------------------------------------------------


def test_both_chlorophyll_sources_down_reports_partial_with_other_two_signals(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(IncoisArgo, "metadata", _fake_argo_metadata)
    monkeypatch.setattr(IncoisArgo, "timeseries", _make_fake_argo_timeseries())
    monkeypatch.setattr(IncoisOceansat, "chlorophyll_series", _fake_oceansat_raises)
    monkeypatch.setattr(OceanColour, "chlorophyll_series", _fake_modis_raises)
    monkeypatch.setattr(OceanColour, "sst_series", _make_fake_sst(_SST_WARM_SERIES, _SST_ANOMALY_SERIES))

    result = get_productivity_history()

    assert result.ok is True
    assert result.partial is True
    assert "chlorophyll_trend" in result.missing
    assert not any(o.variable.startswith("chlorophyll_a") for o in result.observations)

    # Named in ordinary words, never the internal `_`-joined key (constraint 3 — this
    # is the exact bug the rewrite exists to fix).
    assert "chlorophyll_trend" not in result.summary
    assert "chlorophyll" in result.summary

    # The other two signals are unaffected by chlorophyll's failure.
    remaining_trend_vars = {o.variable for o in result.observations if o.variable.endswith("_trend")}
    assert remaining_trend_vars == {"subsurface_temperature_trend", "sea_surface_temperature_trend"}


# --------------------------------------------------------------------------------------
# 4. A chlorophyll slope inside its own data-derived noise floor is reported "stable",
#    not narrated as a trend, and is NOT treated as missing (a stable finding is still a
#    real, reported signal).
# --------------------------------------------------------------------------------------


def test_chlorophyll_slope_inside_noise_floor_is_reported_stable(monkeypatch, tmp_path):
    _patch_all_three(monkeypatch, tmp_path, chl=_make_fake_oceansat_chlorophyll(_CHL_STABLE_SERIES))

    result = get_productivity_history()

    assert result.ok is True
    assert "chlorophyll_trend" not in result.missing, "a 'stable' finding is a real signal, not a missing one"
    chl_obs = [o for o in result.observations if o.variable == "chlorophyll_a_trend"]
    assert len(chl_obs) == 1
    q = chl_obs[0].qualifiers
    assert q["direction"] == "stable"
    assert abs(chl_obs[0].value) <= q["noise_floor_mg_m3_per_decade"]
    assert "stable" in result.summary.lower()


# --------------------------------------------------------------------------------------
# 5. Trend Observation qualifiers carry the real first/last timestamps and a real point
#    count matching the input series -- for all three signals, not just Argo.
# --------------------------------------------------------------------------------------


def test_trend_qualifiers_carry_real_span_and_point_count(monkeypatch, tmp_path):
    _patch_all_three(monkeypatch, tmp_path)

    result = get_productivity_history()

    trend_by_var = {o.variable: o for o in result.observations if o.variable.endswith("_trend")}

    argo_q = trend_by_var["subsurface_temperature_trend"].qualifiers
    assert argo_q["n_points"] == _ARGO_N_POINTS
    assert "obs_start" in argo_q and "obs_end" in argo_q
    assert datetime.fromisoformat(argo_q["obs_start"]) < datetime.fromisoformat(argo_q["obs_end"])

    chl_q = trend_by_var["chlorophyll_a_trend"].qualifiers
    assert chl_q["n_points"] == len(_CHL_DECLINE_SERIES)
    expected_chl_start = datetime(*sorted(_CHL_DECLINE_SERIES)[0].timetuple()[:3], tzinfo=UTC)
    expected_chl_end = datetime(*sorted(_CHL_DECLINE_SERIES)[-1].timetuple()[:3], tzinfo=UTC)
    assert chl_q["obs_start"] == expected_chl_start.isoformat()
    assert chl_q["obs_end"] == expected_chl_end.isoformat()

    sst_q = trend_by_var["sea_surface_temperature_trend"].qualifiers
    assert sst_q["n_points"] == len(_SST_WARM_SERIES)
    expected_sst_start = datetime(*sorted(_SST_WARM_SERIES)[0].timetuple()[:3], tzinfo=UTC)
    expected_sst_end = datetime(*sorted(_SST_WARM_SERIES)[-1].timetuple()[:3], tzinfo=UTC)
    assert sst_q["obs_start"] == expected_sst_start.isoformat()
    assert sst_q["obs_end"] == expected_sst_end.isoformat()


# --------------------------------------------------------------------------------------
# 6. A series that clamps to the Oceansat archive's 2020 end never produces a summary
#    sentence calling it recent or current.
# --------------------------------------------------------------------------------------


def test_clamped_oceansat_archive_is_never_called_recent_or_current(monkeypatch, tmp_path):
    _patch_all_three(
        monkeypatch, tmp_path,
        chl=_make_fake_oceansat_chlorophyll(_CHL_DECLINE_SERIES, clamped=True),
    )

    result = get_productivity_history()

    assert result.ok is True
    chl_obs = [o for o in result.observations if o.variable == "chlorophyll_a_trend"]
    assert len(chl_obs) == 1
    notes_lower = chl_obs[0].provenance.notes.lower()
    assert "clamped" in notes_lower  # the clamp itself is surfaced, not hidden
    assert "2020" in chl_obs[0].provenance.notes  # the real archive end is cited

    summary_lower = result.summary.lower()
    assert "recent" not in summary_lower
    # "current" may appear only as part of "not current" -- never a bare claim of
    # currency for a record that ends in 2020.
    assert re.search(r"(?<!not )current", summary_lower) is None


# --------------------------------------------------------------------------------------
# 7. No numeric token in `summary` is absent from `observations` -- the system's own
#    invariant-3 audit (mirrors test_productive_waters.py / test_pfz_derived_chlorophyll.py).
# --------------------------------------------------------------------------------------


def test_summary_numbers_all_traceable_to_observations(monkeypatch, tmp_path):
    _patch_all_three(monkeypatch, tmp_path)

    result = get_productivity_history()

    assert result.summary, "expected non-empty prose"
    bad = check_unsourced_numbers(result.summary, result.observations)
    assert bad == [], f"summary contains numbers not traceable to any Observation: {bad}"


# --------------------------------------------------------------------------------------
# 8. Cache keys are deterministic across two identical calls and contain no timestamp of
#    "now" -- both as a pure-function guarantee on the key builders themselves (mirrors
#    test_incois_thredds_key.py's ``_binary_key`` regression test) and as an end-to-end
#    reuse check through the real tool call.
# --------------------------------------------------------------------------------------


def test_chl_and_sst_cache_key_builders_take_no_time_argument():
    sig_chl = inspect.signature(_chl_trend_cache_key)
    sig_sst = inspect.signature(_sst_trend_cache_key)
    assert list(sig_chl.parameters) == ["lat", "lon"]
    assert list(sig_sst.parameters) == ["lat", "lon"]


def test_chl_and_sst_cache_keys_are_deterministic_and_point_specific():
    assert _chl_trend_cache_key(9.45, 79.30) == _chl_trend_cache_key(9.45, 79.30)
    assert _sst_trend_cache_key(9.45, 79.30) == _sst_trend_cache_key(9.45, 79.30)
    # Not a constant -- a different point must still produce a different key.
    assert _chl_trend_cache_key(9.45, 79.30) != _chl_trend_cache_key(10.0, 80.0)
    assert _sst_trend_cache_key(9.45, 79.30) != _sst_trend_cache_key(10.0, 80.0)


def test_chl_and_sst_trends_are_cached_and_reused_on_second_call(monkeypatch, tmp_path):
    _patch_all_three(monkeypatch, tmp_path)

    first = get_productivity_history()
    assert first.ok is True
    first_chl = [o for o in first.observations if o.variable == "chlorophyll_a_trend"]
    first_sst = [o for o in first.observations if o.variable == "sea_surface_temperature_trend"]
    assert len(first_chl) == 1 and len(first_sst) == 1

    # A second live call is now impossible for either signal -- if the tool still
    # succeeds with the same values, it must be reusing the cached computation.
    monkeypatch.setattr(IncoisOceansat, "chlorophyll_series", _fake_oceansat_raises)
    monkeypatch.setattr(OceanColour, "chlorophyll_series", _fake_modis_raises)
    monkeypatch.setattr(OceanColour, "sst_series", _fake_sst_raises)

    second = get_productivity_history()

    assert second.ok is True
    second_chl = [o for o in second.observations if o.variable == "chlorophyll_a_trend"]
    second_sst = [o for o in second.observations if o.variable == "sea_surface_temperature_trend"]
    assert len(second_chl) == 1 and second_chl[0].value == first_chl[0].value
    assert len(second_sst) == 1 and second_sst[0].value == first_sst[0].value
    assert "chlorophyll_trend" not in second.missing
    assert "sst_trend" not in second.missing
