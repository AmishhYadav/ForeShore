"""Tests for the chlorophyll fallback chain in ``derive_pfz_zones``
(backend/foreshore/tools/pfz_derived.py).

Live probing (2026-09-06, recorded in the tool's own module docstring) found that the
INCOIS OSF ``chl`` grid never once covers this system's Palk Bay / Gulf of Mannar bbox --
it is a Pacific Islands product. Before this fix, that meant the chlorophyll signal in
``derive_pfz_zones`` had never once fired for this region: every real call fell through
to the SST-only branch. The fix makes chlorophyll acquisition a three-step fallback
chain (INCOIS -> NOAA gap-filled VIIRS -> NASA MODIS, see ``sources/oceancolour.py``),
and these tests exercise every branch of that chain without opening a socket.

No frozen fixture blob exists for ``incois_osf_chl`` or the NOAA CoastWatch products
(``data/fixtures/`` was checked directly), so exercising the chain's actual acquisition
logic -- as opposed to just its honest-abstention path -- means monkeypatching
``IncoisThredds.slice`` and ``OceanColour.chlorophyll_slice`` directly, the same
boundary-mocking approach ``test_productivity.py`` uses for the same two adapters.
``FORESHORE_MODE=fixture`` is already set session-wide by ``conftest.py``; every test
here additionally never calls anything that would reach ``Source.get``/``httpx`` at all,
so no test can open a socket even if the mode were flipped.

A single shared 10x10 synthetic SST field (a sharp step at the lon-index-4/5 boundary,
Palk Bay-ish lat/lon range) is reused everywhere: it produces exactly one real SST
frontal zone via the tool's own unmodified gradient/threshold/polygonise pipeline, which
is what lets these tests assert on ``derive_pfz_zones``'s actual output rather than a
mocked one.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import numpy as np
import pytest

from foreshore.agents.runtime import check_unsourced_numbers
from foreshore.models import Provenance, utcnow
from foreshore.sources.base import SourceError
from foreshore.sources.incois_thredds import GridSlice, IncoisThredds
from foreshore.sources.oceancolour import DATASETS, OceanColour
from foreshore.tools.pfz_derived import _INCOIS_CHL_SOURCE_LABEL, derive_pfz_zones

# --------------------------------------------------------------------------------------
# Synthetic grid -- shared by every test below.
# --------------------------------------------------------------------------------------

#: 10x10, inside the palk_bay_gom bbox ([78.0, 8.0, 80.6, 10.9]) but the exact numbers
#: don't matter -- every adapter call in this file is monkeypatched, so no real bbox
#: intersection is ever computed against them.
_LATS = np.linspace(8.0, 9.8, 10)
_LONS = np.linspace(78.0, 79.8, 10)

#: SST resolution 9_260 m is INCOIS OSF's own documented value (module docstring of
#: incois_thredds.py) -- used here so a test asserting "not the SST grid's resolution"
#: is checking against a realistic number, not an arbitrary one.
_SST_RESOLUTION_M = 9_260.0


def _sst_field() -> np.ndarray:
    """A sharp step at columns 5+ -- one clean frontal boundary, nothing else."""
    field = np.full((10, 10), 28.0)
    field[:, 5:] = 30.5
    return field


def _chl_field() -> np.ndarray:
    field = np.full((10, 10), 0.35)
    field[:, 5:] = 0.55
    return field


def _make_provenance(*, source_id: str, source_name: str, authority: str, resolution_m: float) -> Provenance:
    now = utcnow()
    return Provenance(
        source_id=source_id,
        source_name=source_name,
        authority=authority,
        url=f"https://example.test/{source_id}",
        acquired_at=now,
        issued_at=now,
        valid_from=now,
        valid_to=now + timedelta(days=3),
        spatial_resolution_m=resolution_m,
        is_derived=False,
    )


def _sst_slice() -> GridSlice:
    now = utcnow()
    return GridSlice(
        product="sst",
        variables={"sea_surface_temperature": _sst_field()},
        lats=_LATS,
        lons=_LONS,
        valid_time=now,
        file_date=now.date(),
        local_path=None,
        history="Mww3/ECMWF/With_Data_assimilation/fake_sst.nc",
        provenance=_make_provenance(
            source_id="incois_osf_sst", source_name="INCOIS Ocean State Forecast — sst",
            authority="INCOIS", resolution_m=_SST_RESOLUTION_M,
        ),
    )


def _chl_slice(*, source_id: str, source_name: str, authority: str, resolution_m: float) -> GridSlice:
    now = utcnow()
    return GridSlice(
        product="chl",
        variables={"chlorophyll_a": _chl_field()},
        lats=_LATS,
        lons=_LONS,
        valid_time=now,
        file_date=now.date(),
        local_path=None,
        history=None,
        provenance=_make_provenance(
            source_id=source_id, source_name=source_name, authority=authority, resolution_m=resolution_m,
        ),
    )


#: Real, documented resolutions (sources/oceancolour.py DATASETS) -- distinct from each
#: other and from _SST_RESOLUTION_M, so a test asserting "the chlorophyll grid's own
#: resolution, not the SST grid's" is meaningful.
_INCOIS_CHL_RESOLUTION_M = 4_000.0
_GAPFILLED_RESOLUTION_M = DATASETS["gapfilled"].spatial_resolution_m
_MODIS_RESOLUTION_M = DATASETS["modis"].spatial_resolution_m


def _incois_chl_slice() -> GridSlice:
    return _chl_slice(
        source_id="incois_osf_chl", source_name="INCOIS Ocean State Forecast — chl",
        authority="INCOIS", resolution_m=_INCOIS_CHL_RESOLUTION_M,
    )


def _gapfilled_chl_slice() -> GridSlice:
    return _chl_slice(
        source_id="noaa_coastwatch", source_name=OceanColour.source_name,
        authority="NOAA", resolution_m=_GAPFILLED_RESOLUTION_M,
    )


def _modis_chl_slice() -> GridSlice:
    return _chl_slice(
        source_id="noaa_coastwatch", source_name=OceanColour.source_name,
        authority="NOAA", resolution_m=_MODIS_RESOLUTION_M,
    )


# --------------------------------------------------------------------------------------
# Fakes -- installed on the adapter classes, exactly where derive_pfz_zones looks them
# up (deferred imports resolve to these same class objects at call time).
# --------------------------------------------------------------------------------------


def _install_incois_slice(monkeypatch: pytest.MonkeyPatch, *, chl_outcome: Any) -> None:
    """``chl_outcome`` is ``"ok"`` or an exception instance to raise for product="chl".
    product="sst" always succeeds with the shared synthetic front."""

    def _fake(self: IncoisThredds, product: str, *, variables=None, at=None, bbox=None) -> GridSlice:
        if product == "sst":
            return _sst_slice()
        if product == "chl":
            if chl_outcome == "ok":
                return _incois_chl_slice()
            raise chl_outcome
        raise AssertionError(f"unexpected IncoisThredds.slice product {product!r}")

    monkeypatch.setattr(IncoisThredds, "slice", _fake)


def _install_oceancolour(
    monkeypatch: pytest.MonkeyPatch, *, gapfilled_outcome: Any = None, modis_outcome: Any = None,
    calls: list[str] | None = None,
) -> None:
    """``*_outcome`` of ``None`` means "must never be called" (raises AssertionError if
    it is); ``"ok"`` succeeds; anything else is raised as-is. ``calls`` records every
    product actually requested, in order, for tests that assert on call order."""

    def _fake(self: OceanColour, bbox, *, at=None, product: str = "gapfilled") -> GridSlice:
        if calls is not None:
            calls.append(product)
        if product == "gapfilled":
            if gapfilled_outcome is None:
                raise AssertionError("NOAA gap-filled must not be called in this scenario")
            if gapfilled_outcome == "ok":
                return _gapfilled_chl_slice()
            raise gapfilled_outcome
        if product == "modis":
            if modis_outcome is None:
                raise AssertionError("NOAA MODIS must not be called in this scenario")
            if modis_outcome == "ok":
                return _modis_chl_slice()
            raise modis_outcome
        raise AssertionError(f"unexpected OceanColour.chlorophyll_slice product {product!r}")

    monkeypatch.setattr(OceanColour, "chlorophyll_slice", _fake)


# --------------------------------------------------------------------------------------
# 1. INCOIS succeeding -> NOAA never called, chlorophyll_source names INCOIS.
# --------------------------------------------------------------------------------------


def test_incois_success_never_calls_noaa(monkeypatch):
    _install_incois_slice(monkeypatch, chl_outcome="ok")
    _install_oceancolour(monkeypatch)  # both outcomes None -> any NOAA call fails the test

    result = derive_pfz_zones()

    assert result.ok is True
    assert result.payload["chlorophyll_available"] is True
    assert result.payload["chlorophyll_source"] == _INCOIS_CHL_SOURCE_LABEL
    assert "INCOIS" in result.payload["chlorophyll_source"]


# --------------------------------------------------------------------------------------
# 2. INCOIS raises SourceError(status=400) -> gap-filled NOAA tried and succeeds; MODIS
#    never reached; summary does not say chlorophyll was unavailable.
# --------------------------------------------------------------------------------------


def test_incois_400_falls_back_to_gapfilled_noaa(monkeypatch):
    _install_incois_slice(
        monkeypatch,
        chl_outcome=SourceError("incois_osf_chl", "NCSS 400: bbox outside published grid", status=400),
    )
    calls: list[str] = []
    _install_oceancolour(monkeypatch, gapfilled_outcome="ok", modis_outcome=None, calls=calls)

    result = derive_pfz_zones()

    assert result.ok is True
    assert result.payload["chlorophyll_available"] is True
    assert result.payload["chlorophyll_source"] == DATASETS["gapfilled"].label
    assert "gap-filled" in result.payload["chlorophyll_source"]
    assert calls == ["gapfilled"], "MODIS must not be reached once gap-filled succeeds"
    assert "unavailable" not in result.summary.lower()


# --------------------------------------------------------------------------------------
# 3. INCOIS and gap-filled both fail -> MODIS is tried and succeeds.
# --------------------------------------------------------------------------------------


def test_incois_and_gapfilled_fail_falls_back_to_modis(monkeypatch):
    _install_incois_slice(
        monkeypatch,
        chl_outcome=SourceError("incois_osf_chl", "NCSS 400: bbox outside published grid", status=400),
    )
    calls: list[str] = []
    _install_oceancolour(
        monkeypatch,
        gapfilled_outcome=RuntimeError("simulated ERDDAP outage"),
        modis_outcome="ok",
        calls=calls,
    )

    result = derive_pfz_zones()

    assert result.ok is True
    assert result.payload["chlorophyll_available"] is True
    assert result.payload["chlorophyll_source"] == DATASETS["modis"].label
    assert "MODIS" in result.payload["chlorophyll_source"]
    assert calls == ["gapfilled", "modis"]


# --------------------------------------------------------------------------------------
# 4. All three fail -> ok=True, SST-only zones, honest short reason, full detail.
# --------------------------------------------------------------------------------------


def test_all_three_fail_abstains_honestly_but_keeps_sst_only_zones(monkeypatch):
    _install_incois_slice(
        monkeypatch,
        chl_outcome=SourceError("incois_osf_chl", "NCSS 400: bbox outside published grid", status=400),
    )
    calls: list[str] = []
    _install_oceancolour(
        monkeypatch,
        gapfilled_outcome=RuntimeError("simulated ERDDAP outage"),
        modis_outcome=RuntimeError("simulated ERDDAP outage"),
        calls=calls,
    )

    result = derive_pfz_zones()

    assert result.ok is True
    assert result.payload["chlorophyll_available"] is False
    assert result.payload["chlorophyll_source"] is None
    # SST-only zones are still a valid outcome -- the chain failing must not empty them.
    assert result.payload["zones"]["features"], "expected at least one SST-only zone from the synthetic front"
    assert result.observations, "expected SST-derived observations even with no chlorophyll"

    reason = result.payload["chlorophyll_reason"]
    assert reason, "chlorophyll_reason must be populated when the whole chain fails"
    for bad_token in ("SourceError", "RuntimeError", "Error", "Exception", "Traceback"):
        assert bad_token not in reason, f"chlorophyll_reason leaked an exception-shaped token: {reason!r}"

    detail = result.payload["chlorophyll_reason_detail"]
    assert _INCOIS_CHL_SOURCE_LABEL in detail
    assert DATASETS["gapfilled"].label in detail
    assert DATASETS["modis"].label in detail


# --------------------------------------------------------------------------------------
# 5. Chlorophyll observations carry the chlorophyll source's own spatial_resolution_m,
#    never the SST grid's.
# --------------------------------------------------------------------------------------


def test_chlorophyll_observation_carries_its_own_source_resolution(monkeypatch):
    _install_incois_slice(
        monkeypatch,
        chl_outcome=SourceError("incois_osf_chl", "NCSS 400: bbox outside published grid", status=400),
    )
    _install_oceancolour(monkeypatch, gapfilled_outcome="ok", modis_outcome=None)

    result = derive_pfz_zones()

    chl_obs = [o for o in result.observations if o.variable == "derived_pfz_mean_chlorophyll"]
    assert chl_obs, "expected at least one derived_pfz_mean_chlorophyll observation"
    for obs in chl_obs:
        assert obs.provenance.spatial_resolution_m == _GAPFILLED_RESOLUTION_M
        assert obs.provenance.spatial_resolution_m != _SST_RESOLUTION_M
        assert obs.provenance.source_id == "noaa_coastwatch"
        assert obs.provenance.is_derived is True

    # And every other (SST-derived) observation must still carry the SST grid's own
    # resolution -- the fix must not have blurred the two together.
    sst_obs = [o for o in result.observations if o.variable == "derived_pfz_mean_sst"]
    assert sst_obs
    for obs in sst_obs:
        assert obs.provenance.spatial_resolution_m == _SST_RESOLUTION_M


# --------------------------------------------------------------------------------------
# 6. No numeric token in `summary` is absent from `observations` -- invariant 3, audited
#    the same way the rest of the system audits model-written prose.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chl_outcome, gapfilled_outcome, modis_outcome",
    [
        ("ok", None, None),
        (SourceError("incois_osf_chl", "NCSS 400", status=400), "ok", None),
    ],
)
def test_summary_numbers_are_all_sourced(monkeypatch, chl_outcome, gapfilled_outcome, modis_outcome):
    _install_incois_slice(monkeypatch, chl_outcome=chl_outcome)
    _install_oceancolour(monkeypatch, gapfilled_outcome=gapfilled_outcome, modis_outcome=modis_outcome)

    result = derive_pfz_zones()

    bad = check_unsourced_numbers(result.summary, result.observations)
    assert bad == [], f"summary contains numeric token(s) not traceable to any observation: {bad!r}"


# --------------------------------------------------------------------------------------
# 7. Chlorophyll is still never required -- an SST-only result is ok=True, not partial.
# --------------------------------------------------------------------------------------


def test_sst_only_result_is_not_partial(monkeypatch):
    _install_incois_slice(monkeypatch, chl_outcome=RuntimeError("simulated outage"))
    _install_oceancolour(
        monkeypatch,
        gapfilled_outcome=RuntimeError("simulated outage"),
        modis_outcome=RuntimeError("simulated outage"),
    )

    result = derive_pfz_zones()

    assert result.ok is True
    assert result.partial is False
    assert "sea-surface-temperature front alone" in result.summary
