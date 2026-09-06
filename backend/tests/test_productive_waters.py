"""Tests for tool 18, ``find_productive_waters`` (backend/foreshore/tools/productive_waters.py).

Answers the PS bullet "Which regions show high chlorophyll concentration and favourable
sea surface temperature?" by combining ``OceanColour.chlorophyll_slice`` (NOAA CoastWatch)
with ``IncoisThredds.slice("sst", ...)``. Both adapters are monkeypatched at the class
boundary -- the same approach ``test_productivity.py`` uses -- with small, fully
hand-built :class:`~foreshore.sources.incois_thredds.GridSlice` objects, so the suite
opens no socket even though ``FORESHORE_MODE=fixture`` is already enforced session-wide
by ``conftest.py``.

The synthetic grid is a flat 10x10 lat/lon field with chlorophyll held perfectly uniform
(so the 80th-percentile chlorophyll cutoff passes everywhere and never itself carves out
a shape -- that is exercised implicitly, not what these tests are about) and a handful of
2x2 "favourable SST" blocks dropped into an otherwise too-hot field. Each block is placed
so its centroid lands at a hand-computable lat/lon, which makes the ranking-by-distance
assertions exact rather than approximate.
"""

from __future__ import annotations

import re
from datetime import timedelta

import numpy as np
import pytest

from foreshore.agents.runtime import check_unsourced_numbers
from foreshore.models import Provenance, ToolResult, utcnow
from foreshore.sources.incois_thredds import GridSlice, IncoisThredds
from foreshore.sources.oceancolour import OceanColour
from foreshore.tools import registry
from foreshore.tools.productive_waters import (
    CHLOROPHYLL_PERCENTILE,
    SST_FAVOURABLE_BAND_DEGC,
    find_productive_waters,
)

# --------------------------------------------------------------------------------------
# Synthetic grid: 10x10, 0.2 degree spacing, so block centroids land on clean numbers.
# --------------------------------------------------------------------------------------

_LATS = np.round(np.linspace(8.0, 9.8, 10), 4)
_LONS = np.round(np.linspace(78.0, 79.8, 10), 4)

#: Uniform everywhere -- the 80th-percentile chlorophyll cutoff then equals this value
#: exactly and every cell clears it, so chlorophyll never itself restricts which cells
#: become a zone in these tests. That restriction is entirely the SST band's job below,
#: which is what makes the zone geometry (and therefore the ranking) exactly controllable.
_CHL_VALUE = 1.2

#: Outside :data:`SST_FAVOURABLE_BAND_DEGC` (26.0-30.5) on purpose.
_SST_HOT = 33.0
#: Inside the band.
_SST_FAVOURABLE = 27.0


def _prov(source_id: str, source_name: str, authority: str, url: str, resolution_m: float) -> Provenance:
    now = utcnow()
    return Provenance(
        source_id=source_id, source_name=source_name, authority=authority, url=url,
        acquired_at=now, issued_at=now, valid_from=now - timedelta(hours=1),
        valid_to=now + timedelta(hours=23), spatial_resolution_m=resolution_m,
    )


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    favourable_blocks: tuple[tuple[slice, slice], ...] = (),
    chlorophyll_slice=None,
    sst_slice=None,
) -> None:
    """Patch both adapters' class methods with fakes returning small, hand-built grids.

    ``favourable_blocks`` is a tuple of ``(row_slice, col_slice)`` pairs -- each marks a
    patch of the SST grid as :data:`_SST_FAVOURABLE`; everywhere else stays
    :data:`_SST_HOT`, outside the band. Chlorophyll is uniform everywhere (see
    ``_CHL_VALUE`` above), so only the SST band decides which cells become a zone.
    """
    chl_arr = np.full((10, 10), _CHL_VALUE)
    sst_arr = np.full((10, 10), _SST_HOT)
    for row_sl, col_sl in favourable_blocks:
        sst_arr[row_sl, col_sl] = _SST_FAVOURABLE

    now = utcnow()
    today = now.date()

    chl_gs = GridSlice(
        product="chl", variables={"chlorophyll_a": chl_arr}, lats=_LATS, lons=_LONS,
        valid_time=now, file_date=today, local_path=None, history="fake gap-filled chlorophyll composite",
        provenance=_prov("noaa_coastwatch_fake", "fake NOAA CoastWatch chlorophyll", "NOAA",
                          "https://example.test/chl", 9_277.0),
    )
    sst_gs = GridSlice(
        product="sst", variables={"sea_surface_temperature": sst_arr}, lats=_LATS, lons=_LONS,
        valid_time=now, file_date=today, local_path=None, history="fake OSF SST model",
        provenance=_prov("incois_osf_sst_fake", "fake INCOIS OSF SST", "INCOIS",
                          "https://example.test/sst", 11_000.0),
    )

    def _fake_chlorophyll_slice(self, bbox, *, at=None, product="gapfilled"):
        return chl_gs

    def _fake_sst_slice(self, product, *, variables=None, at=None, bbox=None):
        assert product == "sst"
        return sst_gs

    monkeypatch.setattr(OceanColour, "chlorophyll_slice", chlorophyll_slice or _fake_chlorophyll_slice)
    monkeypatch.setattr(IncoisThredds, "slice", sst_slice or _fake_sst_slice)


# Three favourable 2x2 blocks. Centroids (mean of the two lat/lon values in each slice)
# are hand-computable from _LATS/_LONS above:
#   A -- rows[1:3], cols[1:3] -> lats[1,2]=(8.2,8.4) lons[1,2]=(78.2,78.4) -> centroid (8.3, 78.3)
#   C -- rows[4:6], cols[4:6] -> lats[4,5]=(8.8,9.0)  lons[4,5]=(78.8,79.0) -> centroid (8.9, 78.9)
#   B -- rows[7:9], cols[7:9] -> lats[7,8]=(9.4,9.6)  lons[7,8]=(79.4,79.6) -> centroid (9.5, 79.5)
# From reference point (8.0, 78.0) these are monotonically farther apart in that order:
# A closest, then C, then B farthest.
_BLOCK_A = (slice(1, 3), slice(1, 3))
_BLOCK_C = (slice(4, 6), slice(4, 6))
_BLOCK_B = (slice(7, 9), slice(7, 9))


# --------------------------------------------------------------------------------------
# 1. Ranking by ascending distance, and `limit` honoured.
# --------------------------------------------------------------------------------------


def test_zones_ranked_by_ascending_distance_and_limit_honoured(monkeypatch):
    _install_fakes(monkeypatch, favourable_blocks=(_BLOCK_A, _BLOCK_C, _BLOCK_B))

    result = find_productive_waters(lat=8.0, lon=78.0, limit=2)

    assert result.ok is True
    assert result.partial is False
    features = result.payload["zones"]["features"]
    assert len(features) == 2, "limit=2 must be honoured even though 3 zones qualify"

    ranks = [f["properties"]["zone_rank"] for f in features]
    assert ranks == [1, 2]
    distances = [f["properties"]["distance_nm"] for f in features]
    assert distances == sorted(distances), "zones must come back ranked by ascending distance"

    # The two closest blocks to (8.0, 78.0) are A (8.3, 78.3) and C (8.9, 78.9); B (9.5,
    # 79.5) is farthest and must be dropped by limit=2.
    centroid_lats = [round(f["properties"]["centroid_lat"], 2) for f in features]
    assert centroid_lats == [8.3, 8.9]

    # 3 Observations per returned zone (chlorophyll, SST, distance) x 2 zones.
    assert len(result.observations) == 6
    for obs in result.observations:
        assert obs.provenance.is_derived is True


def test_all_qualifying_zones_returned_when_limit_not_binding(monkeypatch):
    _install_fakes(monkeypatch, favourable_blocks=(_BLOCK_A, _BLOCK_C, _BLOCK_B))

    result = find_productive_waters(lat=8.0, lon=78.0, limit=5)

    assert result.ok is True
    features = result.payload["zones"]["features"]
    assert len(features) == 3
    centroid_lats = [round(f["properties"]["centroid_lat"], 2) for f in features]
    assert centroid_lats == [8.3, 8.9, 9.5], "expected ascending-distance order A, C, B"


# --------------------------------------------------------------------------------------
# 2. Every numeric token in `summary` traces to an Observation (mirrors
#    agents.runtime.check_unsourced_numbers, the system's own invariant-3 audit).
# --------------------------------------------------------------------------------------


def test_summary_numbers_all_traceable_to_observations(monkeypatch):
    _install_fakes(monkeypatch, favourable_blocks=(_BLOCK_A, _BLOCK_C, _BLOCK_B))

    result = find_productive_waters(lat=8.0, lon=78.0, limit=5)

    assert result.ok is True
    assert result.summary, "expected non-empty prose"
    bad = check_unsourced_numbers(result.summary, result.observations)
    assert bad == [], f"summary contains numbers not traceable to any Observation: {bad}"


# --------------------------------------------------------------------------------------
# 3. `summary` is clean user-facing prose: no dataset ids, no internal identifiers, no
#    tool names, no exception class names, no enum-looking uppercase tokens.
# --------------------------------------------------------------------------------------


def test_summary_has_no_internal_identifiers_tool_names_or_enum_codes(monkeypatch):
    _install_fakes(monkeypatch, favourable_blocks=(_BLOCK_A, _BLOCK_C, _BLOCK_B))

    result = find_productive_waters(lat=8.0, lon=78.0, limit=5)
    summary = result.summary

    # Every internal identifier in this codebase (tool names, dataset ids, enum values
    # like DO_NOT_ADVISE) is written with an underscore; plain English prose has none.
    assert "_" not in summary, "summary must not contain a dataset id or internal identifier"

    for tool_name in registry.names():
        assert tool_name not in summary, f"internal tool name {tool_name!r} leaked into summary"

    assert "Error" not in summary and "Exception" not in summary, "no exception class name in summary"

    forbidden_enum_codes = {
        "MPA", "BREACH", "HAZARD_EXCLUSION", "ECO_SENSITIVE", "USER_DEFINED",
        "IMBL_HISTORIC_WATERS", "IMBL_MARITIME_BOUNDARY", "DO_NOT_ADVISE",
        "GO_WITH_CAUTION", "CRITICAL", "WARN", "INFO",
    }
    for code in forbidden_enum_codes:
        assert code not in summary

    # INCOIS/NOAA are real agency names, FORESHORE is this system's own product name, and
    # INDICATIVE is a deliberate emphasis word (see pfz_derived.py's own summaries, which
    # use all three the same way) -- the only uppercase-run tokens allowed here.
    allowed_uppercase = {"INCOIS", "NOAA", "FORESHORE", "INDICATIVE"}
    for token in re.findall(r"\b[A-Z]{3,}\b", summary):
        assert token in allowed_uppercase, f"unexpected uppercase enum-looking token {token!r} in summary"


# --------------------------------------------------------------------------------------
# 4. Chlorophyll unavailable -> partial=True, missing=["chlorophyll"], honest summary.
# --------------------------------------------------------------------------------------


def test_chlorophyll_missing_degrades_honestly_and_never_answers_on_sst_alone(monkeypatch):
    def _raise(self, bbox, *, at=None, product="gapfilled"):
        raise RuntimeError("simulated ERDDAP outage")

    _install_fakes(monkeypatch, favourable_blocks=(_BLOCK_A,), chlorophyll_slice=_raise)

    result = find_productive_waters()

    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.partial is True
    assert result.missing == ["chlorophyll"]
    assert result.observations == []
    assert result.payload["zones"] == {"type": "FeatureCollection", "features": []}
    # Must not claim to have found anything -- no zone description language at all.
    assert "averaging" not in result.summary
    assert "Ranked by distance" not in result.summary
    assert "could not be made" in result.summary
    assert "chlorophyll" in result.summary.lower()


def test_sst_missing_degrades_honestly(monkeypatch):
    def _raise(self, product, *, variables=None, at=None, bbox=None):
        raise RuntimeError("simulated THREDDS outage")

    _install_fakes(monkeypatch, favourable_blocks=(_BLOCK_A,), sst_slice=_raise)

    result = find_productive_waters()

    assert result.ok is True
    assert result.partial is True
    assert result.missing == ["sea_surface_temperature"]
    assert result.observations == []
    assert "averaging" not in result.summary
    assert "could not be made" in result.summary


# --------------------------------------------------------------------------------------
# 5. No zone clears both thresholds -> valid empty result, never an error.
# --------------------------------------------------------------------------------------


def test_no_zones_clearing_thresholds_is_a_valid_empty_result_not_an_error(monkeypatch):
    _install_fakes(monkeypatch, favourable_blocks=())  # nothing anywhere inside the favourable SST band

    result = find_productive_waters(lat=8.0, lon=78.0)

    assert result.ok is True
    assert result.partial is False
    assert result.payload["zones"] == {"type": "FeatureCollection", "features": []}
    assert result.observations == []
    assert "stand out" in result.summary


# --------------------------------------------------------------------------------------
# 6. Registration: tool 18, resolves via the shared registry.
# --------------------------------------------------------------------------------------


def test_registered_as_tool_18():
    spec = registry.get("find_productive_waters")
    assert spec.number == 18
    assert spec.handler is find_productive_waters
    assert "find_productive_waters" in registry.names()


# --------------------------------------------------------------------------------------
# 7. Sanity on the module constants the fake grids above were designed against.
# --------------------------------------------------------------------------------------


def test_favourable_sst_band_and_chlorophyll_percentile_are_the_documented_defaults():
    assert SST_FAVOURABLE_BAND_DEGC == (26.0, 30.5)
    assert CHLOROPHYLL_PERCENTILE == 80.0
