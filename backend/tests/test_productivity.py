"""Tests for tool 13, ``get_productivity_history`` (backend/foreshore/tools/productivity.py).

The repository ships no frozen fixtures yet (``data/fixtures/`` is empty — see
``test_provenance.py``'s module docstring), so a call through the real, unmocked
registry in ``FORESHORE_MODE=fixture`` can only exercise the *abstention* path: all
three adapters raise ``FixtureMissing``/``SourceError`` before any number is produced.
That path is real and is tested directly below
(``test_real_registry_call_abstains_honestly_with_no_fixtures``) — it is exactly the
same graceful-degradation contract the tool must satisfy on a dead upstream endpoint.

To exercise the actual trend/caching/honesty logic inside ``productivity.py`` — which is
the point of this module — the tests monkeypatch ``IncoisArgo.metadata``/``.timeseries``
directly with small, realistic synthetic series, following the same boundary-mocking
approach ``test_provenance.py`` uses to exercise ``verdict/engine.py`` without a live
fixture. Every ``Observation`` the fakes return still carries a real, fully-populated
``Provenance`` — the fakes are standing in for the network round-trip, not for the
provenance contract.

**On the ``IncoisThredds`` fakes below.** This module originally wired the chlorophyll
and SST signals to ``IncoisThredds`` (INCOIS's live ``osf/chl``/``osf/sst`` rolling
composites) and this file's fakes patched that adapter accordingly. Both were replaced:
``osf/chl`` never covered Palk Bay at all (a Pacific Islands Countries grid) and
``osf/sst`` is a forward-looking forecast nest with no history to trend over —
``productivity.py`` now reads chlorophyll from ISRO's own Oceansat-2 archive
(``IncoisOceansat``, falling back to NOAA MODIS via ``OceanColour``) and SST from NOAA
OISST v2.1 (also ``OceanColour``), both genuine multi-year records. The ``IncoisThredds``
fakes and constants below (``_CHL_DATES``, ``_SST_VALUES``, ``_make_fake_catalog_dates``,
``_make_fake_point``, and ``_patch_realistic_sources``'s own thredds patching) are now
inert as far as ``get_productivity_history`` is concerned — the tool never calls
``IncoisThredds`` any more — but are left in place rather than deleted: several tests
below still invoke ``_patch_realistic_sources`` for its Argo fakes and CACHE_DIR
isolation, and monkeypatching an adapter method nothing calls is harmless. The tests
that used to exercise chlorophyll/SST behaviour through these thredds fakes have been
retargeted below to the real new adapters (``IncoisOceansat``/``OceanColour``) — see
each test's docstring for what changed and why.

Each test that touches the Argo trend cache isolates ``store/cache.py``'s ``CACHE_DIR``
to a fresh ``tmp_path`` so tests never see each other's cached computation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest

from foreshore.models import UTC, Observation, Provenance, utcnow
from foreshore.sources.incois_erddap import MAX_TIMESERIES_SPAN_DAYS, IncoisArgo, IncoisOceansat
from foreshore.sources.incois_thredds import PRODUCT_CANONICAL_VARS, UNITS, IncoisThredds
from foreshore.sources.oceancolour import OceanColour
from foreshore.store import cache as cache_store
from foreshore.tools import registry
from foreshore.tools.productivity import get_productivity_history

# --------------------------------------------------------------------------------------
# Synthetic, realistic fake adapter data.
# --------------------------------------------------------------------------------------

#: ~9-year synthetic Argo series (20 points), a real warming signal (~0.4 degC/decade)
#: -- large enough to clear ARGO_STABLE_EPSILON_C_PER_DECADE so the trend is unambiguous.
_ARGO_N_POINTS = 20
_ARGO_WARMING_C_PER_DECADE = 0.4

#: Chlorophyll: 3 real cataloged dates, a genuine 3-day rolling composite (matches
#: CLAUDE.md's documented cadence for this product).
_CHL_DATES = [date(2026, 8, 27), date(2026, 8, 28), date(2026, 8, 29)]
_CHL_VALUES = {date(2026, 8, 27): 0.42, date(2026, 8, 28): 0.40, date(2026, 8, 29): 0.37}

#: SST: 2 real cataloged dates.
_SST_DATES = [date(2026, 8, 28), date(2026, 8, 29)]
_SST_VALUES = {date(2026, 8, 28): 29.1, date(2026, 8, 29): 29.4}


def _fake_argo_metadata(self: IncoisArgo) -> dict[str, Any]:
    return {"depth_range": (0.0, 2000.0), "dataset_id": "incois_argo_10d_VAM"}


def _make_fake_argo_timeseries(n_points: int = _ARGO_N_POINTS, warming_c_per_decade: float = _ARGO_WARMING_C_PER_DECADE):
    def _fake(self: IncoisArgo, lat: float, lon: float, depth_m: float, start: datetime, end: datetime) -> list[Observation]:
        times = [start + (end - start) * (i / (n_points - 1)) for i in range(n_points)]
        base_temp = 28.0
        total_days = (end - start).total_seconds() / 86400.0
        prov = Provenance(
            source_id="incois_argo",
            source_name="INCOIS gridded Argo 10-day objective analysis (incois_argo_10d_VAM)",
            authority="INCOIS",
            url="https://erddap.incois.gov.in/erddap/griddap/incois_argo_10d_VAM.csv?fake",
            acquired_at=utcnow(),
            issued_at=times[-1],
            valid_from=times[0],
            valid_to=times[-1] + timedelta(days=15),
            spatial_resolution_m=111_000.0,
        )
        out = []
        for t in times:
            elapsed_days = (t - start).total_seconds() / 86400.0
            value = base_temp + (warming_c_per_decade / 3652.5) * elapsed_days
            out.append(Observation(
                variable="subsurface_temperature", value=value, unit="degs",
                lat=lat, lon=lon, valid_time=t, provenance=prov,
                qualifiers={"grid_lat": lat, "grid_lon": lon, "depth_m": depth_m, "requested_lat": lat, "requested_lon": lon},
            ))
        return out

    return _fake


def _fake_argo_timeseries_empty(self: IncoisArgo, lat: float, lon: float, depth_m: float, start: datetime, end: datetime) -> list[Observation]:
    return []


def _fake_argo_timeseries_raises(self: IncoisArgo, lat: float, lon: float, depth_m: float, start: datetime, end: datetime) -> list[Observation]:
    raise RuntimeError("simulated ERDDAP outage")


def _fake_argo_metadata_raises(self: IncoisArgo) -> dict[str, Any]:
    raise RuntimeError("simulated ERDDAP metadata outage")


def _make_fake_catalog_dates(dates_by_product: dict[str, list[date]]):
    def _fake(self: IncoisThredds, product: str) -> list[date]:
        return list(dates_by_product.get(product, []))

    return _fake


def _fake_catalog_dates_raises(self: IncoisThredds, product: str) -> list[date]:
    raise RuntimeError("simulated THREDDS catalogue outage")


def _make_fake_point(values_by_product: dict[str, dict[date, float]]):
    def _fake(self: IncoisThredds, product: str, lat: float, lon: float, *, at: datetime | None = None, variables=None) -> list[Observation]:
        assert at is not None
        d = at.date()
        value = values_by_product.get(product, {}).get(d)
        if value is None:
            return []
        canonical_var = PRODUCT_CANONICAL_VARS[product][0]
        issued = datetime(d.year, d.month, d.day, tzinfo=UTC)
        span = timedelta(days=3) if product == "chl" else timedelta(days=7)
        prov = Provenance(
            source_id=f"incois_osf_{product}",
            source_name=f"INCOIS Ocean State Forecast (MWW3/ECMWF) — {product}",
            authority="INCOIS",
            url=f"https://incois.gov.in/thredds/ncss/grid/fake_{product}_{d.isoformat()}.nc",
            acquired_at=utcnow(),
            issued_at=issued,
            valid_from=issued,
            valid_to=issued + span,
            spatial_resolution_m=4_000.0 if product == "chl" else 9_260.0,
        )
        return [Observation(
            variable=canonical_var, value=value, unit=UNITS[canonical_var],
            lat=lat, lon=lon, valid_time=issued, provenance=prov, qualifiers={},
        )]

    return _fake


# --------------------------------------------------------------------------------------
# Fakes for the current chlorophyll/SST adapters (IncoisOceansat, OceanColour) --
# these replace the IncoisThredds fakes above for the tests that actually exercise
# chlorophyll/SST behaviour post-rewrite.
# --------------------------------------------------------------------------------------


def _make_fake_oceansat_chlorophyll_series(values_by_date: dict[date, float]):
    """A small, realistic ``IncoisOceansat.chlorophyll_series`` fake: one Observation per
    date in ``values_by_date`` that falls within ``[start, end]``, each carrying a real
    ISRO/NRSC ``Provenance`` -- the fake stands in for the network round-trip, not for
    the provenance contract."""

    def _fake(self: IncoisOceansat, lat: float, lon: float, *, start: datetime, end: datetime) -> list[Observation]:
        out: list[Observation] = []
        for d in sorted(values_by_date):
            dt = datetime(d.year, d.month, d.day, tzinfo=UTC)
            if dt < start or dt > end:
                continue
            prov = Provenance(
                source_id="incois_oceansat2",
                source_name="INCOIS / ISRO Oceansat-2 Ocean Colour Monitor (incois_oceansat2_datasets)",
                authority="ISRO/NRSC",
                url="https://erddap.incois.gov.in/erddap/griddap/incois_oceansat2_datasets.csv?fake",
                acquired_at=utcnow(),
                issued_at=dt, valid_from=dt, valid_to=dt,
                spatial_resolution_m=4_320.0,
            )
            out.append(Observation(
                variable="chlorophyll_a", value=values_by_date[d], unit="mg/m^3",
                lat=lat, lon=lon, valid_time=dt, provenance=prov,
                qualifiers={
                    "grid_lat": lat, "grid_lon": lon,
                    "requested_lat": lat, "requested_lon": lon,
                    "time_range_clamped": False,
                },
            ))
        return out

    return _fake


def _fake_oceansat_chlorophyll_series_raises(self: IncoisOceansat, lat: float, lon: float, *, start: datetime, end: datetime) -> list[Observation]:
    raise RuntimeError("simulated Oceansat ERDDAP outage")


def _fake_oceancolour_chlorophyll_series_raises(self: OceanColour, lat: float, lon: float, *, start: datetime, end: datetime, product: str = "gapfilled") -> list[Observation]:
    raise RuntimeError("simulated NOAA MODIS outage")


def _fake_oceancolour_sst_series_raises(self: OceanColour, lat: float, lon: float, *, start: datetime, end: datetime) -> list[Observation]:
    raise RuntimeError("simulated OISST outage")


def _patch_realistic_sources(monkeypatch: pytest.MonkeyPatch, tmp_path, *, argo_metadata=None, argo_timeseries=None, catalog_dates=None, point=None) -> None:
    """Isolate the productivity-trend cache to ``tmp_path`` and install realistic (or
    caller-overridden) fakes for both adapters' relevant methods."""
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(IncoisArgo, "metadata", argo_metadata or _fake_argo_metadata)
    monkeypatch.setattr(IncoisArgo, "timeseries", argo_timeseries or _make_fake_argo_timeseries())
    monkeypatch.setattr(
        IncoisThredds, "catalog_dates",
        catalog_dates or _make_fake_catalog_dates({"chl": _CHL_DATES, "sst": _SST_DATES}),
    )
    monkeypatch.setattr(
        IncoisThredds, "point",
        point or _make_fake_point({"chl": _CHL_VALUES, "sst": _SST_VALUES}),
    )


# --------------------------------------------------------------------------------------
# 1. Defaults: ok=True, every Observation carries a real Provenance.
# --------------------------------------------------------------------------------------


def test_defaults_return_ok_and_every_observation_has_real_provenance(monkeypatch, tmp_path):
    _patch_realistic_sources(monkeypatch, tmp_path)

    result = get_productivity_history()

    assert result.ok is True
    assert result.observations, "expected at least one Observation with realistic fakes wired up"
    for obs in result.observations:
        assert isinstance(obs, Observation)
        prov = obs.provenance
        assert isinstance(prov, Provenance)
        assert prov.source_id, "every observation must trace to a non-empty source_id"
        assert prov.url, "every observation must carry a source url"
        assert obs.valid_time is not None and obs.valid_time.tzinfo is not None
        assert prov.valid_from is not None and prov.valid_from.tzinfo is not None
        assert prov.acquired_at is not None and prov.acquired_at.tzinfo is not None
        # invariant 3 — no unsourced numbers: derived stats must say they are derived.
        if prov.is_derived:
            assert prov.notes, f"{obs.variable}: a derived observation must name its lineage"


# --------------------------------------------------------------------------------------
# 2. Chlorophyll provenance for a closed historical archive must say so plainly, never
#    "recent"/"current" -- the inverse of what this slot originally tested. The old
#    IncoisThredds-backed chlorophyll/SST signals really were short rolling windows that
#    had to be *prevented* from reading as multi-year; the new Oceansat/OISST signals are
#    genuinely multi-year, so the surviving honesty obligation flips: a signal that is
#    real and multi-year but historical (Oceansat's archive ends 2020-05-01) must never
#    be narrated as if it describes present conditions. Retargeted to the real adapter
#    that replaced IncoisThredds here.
# --------------------------------------------------------------------------------------


def test_chl_provenance_honestly_labels_the_closed_historical_archive(monkeypatch, tmp_path):
    _patch_realistic_sources(monkeypatch, tmp_path)  # Argo fakes + CACHE_DIR isolation
    monkeypatch.setattr(
        IncoisOceansat, "chlorophyll_series",
        _make_fake_oceansat_chlorophyll_series({
            date(2018, 1, 1): 0.40, date(2018, 2, 1): 0.42, date(2018, 3, 1): 0.39,
        }),
    )
    monkeypatch.setattr(OceanColour, "sst_series", _fake_oceancolour_sst_series_raises)

    result = get_productivity_history()

    chl_obs = [o for o in result.observations if o.variable.startswith("chlorophyll_a")]
    assert chl_obs, "expected a chlorophyll observation from the Oceansat fake"
    prov = chl_obs[0].provenance
    assert prov.is_derived is True
    # Honestly cites the real archive end and says plainly it is not current.
    assert "2020" in prov.notes
    assert "not current" in prov.notes.lower() or "closed" in prov.notes.lower()
    # And the user-facing summary must never call a 2011-2020 archive recent/current.
    summary_lower = result.summary.lower()
    assert "recent" not in summary_lower
    assert "current" not in summary_lower or "not current" in summary_lower


def test_chl_single_point_is_reported_as_a_level_not_a_fabricated_trend(monkeypatch, tmp_path):
    """Only one real chlorophyll date retrieved -> a 'level' reading, never a slope (and
    never a noise floor, which needs a residual to estimate from a single point).
    Retargeted from the old IncoisThredds rolling-window signal to the real Oceansat
    signal that replaced it -- the invariant under test (a single point is never
    narrated as a trend) is unchanged."""
    _patch_realistic_sources(monkeypatch, tmp_path)
    single_date = {date(2018, 6, 15): 0.37}
    monkeypatch.setattr(
        IncoisOceansat, "chlorophyll_series", _make_fake_oceansat_chlorophyll_series(single_date),
    )

    result = get_productivity_history()

    assert result.ok is True
    assert "chlorophyll_trend" in result.missing, "a single point is not a trend and must be reported missing"
    level_obs = [o for o in result.observations if o.variable == "chlorophyll_a_level"]
    assert len(level_obs) == 1
    assert level_obs[0].qualifiers.get("n_points") == 1
    assert "insufficient" in level_obs[0].qualifiers.get("note", "").lower()


# --------------------------------------------------------------------------------------
# 3a. Real, unmocked registry call — proves honest abstention with zero fixtures.
# --------------------------------------------------------------------------------------


def test_real_registry_call_abstains_honestly_with_no_fixtures(tmp_path, monkeypatch):
    """Called through the real, unmocked registry in FORESHORE_MODE=fixture (set
    session-wide by conftest.py) against an empty fixture directory, all three adapters
    must fail cleanly and the tool must degrade to an honest abstention — never a crash,
    never an invented narrative.

    Isolates *both* CACHE_DIR and FIXTURE_DIR to this test's own empty tmp_path.
    `scripts/freeze_fixtures.py` now legitimately populates the repo's real
    `data/fixtures/` (Phase 8) — including a real `productivity_trends` entry this tool
    itself reads — so patching only CACHE_DIR (as this test originally did, when the
    repo's fixtures directory really was still empty) would let a real frozen
    observation leak in and falsify this test's actual premise, "nothing is available
    anywhere." That the *repo* now has real fixtures is progress, not a reason to weaken
    this test — it still needs a genuinely empty fixture directory to exercise the
    abstention path at all."""
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cache_store, "FIXTURE_DIR", tmp_path / "fixtures")

    result = registry.call("get_productivity_history", {})

    assert result.ok is True
    assert result.partial is True
    assert set(result.missing) == {"argo_subsurface_trend", "chlorophyll_trend", "sst_trend"}
    assert result.observations == []
    assert "insufficient data for a productivity diagnostic" in result.summary


# --------------------------------------------------------------------------------------
# 3b. Argo trend, when data permits, is honestly reported as multi-year scale.
# --------------------------------------------------------------------------------------


def test_argo_trend_reports_a_real_multiyear_span_when_data_permits(monkeypatch, tmp_path):
    _patch_realistic_sources(monkeypatch, tmp_path)

    result = get_productivity_history()

    trend_obs = [o for o in result.observations if o.variable == "subsurface_temperature_trend"]
    assert len(trend_obs) == 1, "expected exactly one Argo trend observation from the synthetic multi-year series"
    obs = trend_obs[0]
    prov = obs.provenance
    assert prov.is_derived is True
    span_days = (prov.valid_to - prov.valid_from).days
    assert span_days > 365, f"expected a genuine multi-year Argo span, got {span_days} days"
    assert obs.qualifiers["direction"] == "warming"
    assert obs.value > 0  # positive slope, degC/decade
    # default years=10 exceeds MAX_TIMESERIES_SPAN_DAYS (~9 years) -- the tool must say so.
    assert 365.25 * 10 > MAX_TIMESERIES_SPAN_DAYS
    assert "clamped" in prov.notes.lower()


def test_argo_trend_missing_honestly_when_series_is_empty(monkeypatch, tmp_path):
    _patch_realistic_sources(monkeypatch, tmp_path, argo_timeseries=_fake_argo_timeseries_empty)

    result = get_productivity_history()

    assert result.ok is True
    assert "argo_subsurface_trend" in result.missing
    assert not any(o.variable.startswith("subsurface_temperature") for o in result.observations)


# --------------------------------------------------------------------------------------
# 4. Caching: the second call reuses the cached Argo trend computation.
# --------------------------------------------------------------------------------------


def test_argo_trend_is_cached_and_reused_on_second_call(monkeypatch, tmp_path):
    _patch_realistic_sources(monkeypatch, tmp_path)

    first = get_productivity_history()
    assert first.ok is True
    first_trend = [o for o in first.observations if o.variable == "subsurface_temperature_trend"]
    assert len(first_trend) == 1

    # Now make a second live call impossible -- if the tool still succeeds, it must be
    # because it read the cached computation, not because it refetched.
    monkeypatch.setattr(IncoisArgo, "timeseries", _fake_argo_timeseries_raises)

    second = get_productivity_history()

    assert second.ok is True
    second_trend = [o for o in second.observations if o.variable == "subsurface_temperature_trend"]
    assert len(second_trend) == 1, "expected the cached Argo trend to be reused, not recomputed"
    assert second_trend[0].value == first_trend[0].value
    assert "argo_subsurface_trend" not in second.missing


# --------------------------------------------------------------------------------------
# 5. Graceful degradation when all three adapters fail outright. Retargeted: the old
#    version only had to kill IncoisThredds once to take out both chlorophyll and SST
#    (they shared one adapter); the new signals are wired to two different adapters
#    (IncoisOceansat + OceanColour) and chlorophyll itself has an internal MODIS
#    fallback, so all three of those must fail before chlorophyll is truly missing.
# --------------------------------------------------------------------------------------


def test_degrades_gracefully_when_both_adapters_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(IncoisArgo, "metadata", _fake_argo_metadata_raises)
    monkeypatch.setattr(IncoisArgo, "timeseries", _fake_argo_timeseries_raises)
    monkeypatch.setattr(IncoisOceansat, "chlorophyll_series", _fake_oceansat_chlorophyll_series_raises)
    monkeypatch.setattr(OceanColour, "chlorophyll_series", _fake_oceancolour_chlorophyll_series_raises)
    monkeypatch.setattr(OceanColour, "sst_series", _fake_oceancolour_sst_series_raises)

    result = get_productivity_history()

    assert result.ok is True
    assert result.partial is True
    assert set(result.missing) == {"argo_subsurface_trend", "chlorophyll_trend", "sst_trend"}
    assert result.observations == []
    assert "insufficient data for a productivity diagnostic" in result.summary
    diagnostics = result.payload.get("diagnostics", {})
    assert "simulated ERDDAP outage" in diagnostics.get("argo_subsurface_trend", "")
    assert "simulated Oceansat ERDDAP outage" in diagnostics.get("chlorophyll_trend", "")
    assert "simulated NOAA MODIS outage" in diagnostics.get("chlorophyll_trend", "")
    assert "simulated OISST outage" in diagnostics.get("sst_trend", "")
    # Ordinary-language naming (constraint 3: no `_`-joined internal identifiers in a
    # tool's user-facing summary) -- the exact bug this whole rewrite exists to fix.
    assert "chl_recent_trend" not in result.summary
    assert "sst_recent_trend" not in result.summary
    assert "chlorophyll" in result.summary
    assert "sea-surface temperature" in result.summary
