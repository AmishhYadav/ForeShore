"""Tests for ``backend/foreshore/sources/oceancolour.py`` — the NOAA CoastWatch ERDDAP
adapter for chlorophyll and the long OISST record.

Every test here drives the module's pure logic (constraint-string assembly, the CSV
grid pivot, cache-key hashing, time-range clamping) with hand-written literals. Nothing
opens a socket: the whole suite runs under ``FORESHORE_MODE=fixture`` (forced by
``conftest.py``), and none of these tests call ``Source.get`` at all — the private
helpers under test never touch the network themselves, only the public
``chlorophyll_slice``/``chlorophyll_series``/``sst_series``/``metadata``/``health``
methods do, and those are exercised separately, live, by a throwaway script (not a
test) per the brief.

The exact constraint strings asserted below are lifted directly from the module's own
docstring (``[(last)][(0.0)][(10.9):(8.0)][(78.0):(80.6)]`` for the gap-filled dataset)
so a change to the builder that drifts from the documented example fails loudly here,
not on the next live probe.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from foreshore.sources.base import SourceError
from foreshore.sources.oceancolour import (
    DATASETS,
    OceanColour,
    _cache_key,
    _clamp_time_range,
    _grid_dims_expr,
    _is_missing,
    _parse_csv_text,
    _parse_das,
    _point_dims_expr,
)

UTC = timezone.utc

#: The region's own bbox (config/regions/palk_bay_gom.yaml), used verbatim so the
#: asserted strings match the module docstring's own worked example exactly.
BBOX = (78.0, 8.0, 80.6, 10.9)


# --------------------------------------------------------------------------------------
# Constraint building — one assertion per dataset, exact string, per CLAUDE.md's
# "Verified live facts": the two chlorophyll grids run north-to-south (descending
# latitude), OISST runs south-to-north (ascending).
# --------------------------------------------------------------------------------------


def test_gapfilled_constraint_is_descending_latitude_with_degenerate_altitude():
    spec = DATASETS["gapfilled"]
    expr = _grid_dims_expr(spec, bbox=BBOX, lat_ascending=False, time_expr="(last)")
    assert expr == "[(last)][(0.0)][(10.9):(8.0)][(78.0):(80.6)]"


def test_modis_constraint_is_descending_latitude_with_no_degenerate_axis():
    spec = DATASETS["modis"]
    expr = _grid_dims_expr(spec, bbox=BBOX, lat_ascending=False, time_expr="(last)")
    assert expr == "[(last)][(10.9):(8.0)][(78.0):(80.6)]"
    # MODIS has no degenerate axis at all — confirm no stray "(0.0)" leaked in.
    assert "(0.0)" not in expr


def test_oisst_constraint_is_ascending_latitude_with_degenerate_zlev():
    spec = DATASETS["sst"]
    expr = _grid_dims_expr(spec, bbox=BBOX, lat_ascending=True, time_expr="(last)")
    assert expr == "[(last)][(0.0)][(8.0):(10.9)][(78.0):(80.6)]"


def test_same_bbox_diverges_only_in_latitude_direction_between_oisst_and_chlorophyll():
    """The single most important behaviour this module encodes: the same bbox produces
    a *different* latitude constraint string depending on which dataset's own axis
    direction was read from its .das — never a hardcoded per-dataset direction."""
    chl_expr = _grid_dims_expr(DATASETS["gapfilled"], bbox=BBOX, lat_ascending=False, time_expr="(last)")
    sst_expr = _grid_dims_expr(DATASETS["sst"], bbox=BBOX, lat_ascending=True, time_expr="(last)")
    assert "(10.9):(8.0)" in chl_expr
    assert "(8.0):(10.9)" in sst_expr
    assert "(10.9):(8.0)" not in sst_expr
    assert "(8.0):(10.9)" not in chl_expr


def test_point_constraint_needs_no_direction():
    """A point query (the *_series methods) is a single coordinate, not a range — no
    ascending/descending decision applies, unlike the bbox-range builder above."""
    spec = DATASETS["sst"]
    expr = _point_dims_expr(spec, lat=9.45, lon=79.3, time_expr="(2020-01-01T00:00:00Z):(2020-01-02T00:00:00Z)")
    assert expr == "[(2020-01-01T00:00:00Z):(2020-01-02T00:00:00Z)][(0.0)][(9.45)][(79.3)]"


# --------------------------------------------------------------------------------------
# Degenerate-axis handling, read straight off the fixed DATASETS contract.
# --------------------------------------------------------------------------------------


def test_degenerate_axis_table_matches_the_documented_contract():
    assert DATASETS["gapfilled"].degenerate_axis_value == {"altitude": "(0.0)"}
    assert DATASETS["sst"].degenerate_axis_value == {"zlev": "(0.0)"}
    assert DATASETS["modis"].degenerate_axis_value == {}


# --------------------------------------------------------------------------------------
# Axis direction read from .das, never hardcoded — a hand-written .das snippet for a
# descending-latitude dataset and one for an ascending-latitude dataset.
# --------------------------------------------------------------------------------------

_DAS_DESCENDING = """Attributes {
  NC_GLOBAL {
    String time_coverage_start "2021-08-13T12:00:00Z";
    String time_coverage_end "2026-09-03T12:00:00Z";
  }
  latitude {
    Float64 actual_range 89.97916666666667, -89.97916666666667;
    String axis "Y";
  }
  longitude {
    Float64 actual_range -179.9791666666667, 179.9791666666667;
    String axis "X";
  }
}
"""

_DAS_ASCENDING = """Attributes {
  NC_GLOBAL {
    String time_coverage_start "1981-09-01T00:00:00Z";
    String time_coverage_end "2026-08-21T12:00:00Z";
  }
  latitude {
    Float64 actual_range -89.875, 89.875;
    String axis "Y";
  }
  longitude {
    Float64 actual_range 0.125, 359.875;
    String axis "X";
  }
}
"""


def test_das_actual_range_preserves_descending_storage_order():
    das = _parse_das(_DAS_DESCENDING)
    lo, hi = das["latitude"]["actual_range"]
    assert (lo, hi) == (89.97916666666667, -89.97916666666667)
    assert lo > hi  # descending — this is what lat_ascending=False must be derived from


def test_das_actual_range_preserves_ascending_storage_order():
    das = _parse_das(_DAS_ASCENDING)
    lo, hi = das["latitude"]["actual_range"]
    assert (lo, hi) == (-89.875, 89.875)
    assert lo < hi


def test_das_global_attrs_carry_time_coverage():
    das = _parse_das(_DAS_ASCENDING)
    assert das["NC_GLOBAL"]["time_coverage_start"] == "1981-09-01T00:00:00Z"
    assert das["NC_GLOBAL"]["time_coverage_end"] == "2026-08-21T12:00:00Z"


# --------------------------------------------------------------------------------------
# CSV -> 2-D array pivot. Six rows over a 2x3 grid (2 latitudes x 3 longitudes), one
# cell NaN, latitude rows arriving in descending order (as the real gap-filled/MODIS
# datasets do) to confirm the flip-to-ascending behaviour.
# --------------------------------------------------------------------------------------

_GRID_CSV = (
    "time,altitude,latitude,longitude,chlor_a\n"
    "UTC,m,degrees_north,degrees_east,mg m-3\n"
    "2026-09-03T12:00:00Z,0.0,9.0,78.0,0.30\n"
    "2026-09-03T12:00:00Z,0.0,9.0,78.1,0.40\n"
    "2026-09-03T12:00:00Z,0.0,9.0,78.2,0.50\n"
    "2026-09-03T12:00:00Z,0.0,8.0,78.0,0.10\n"
    "2026-09-03T12:00:00Z,0.0,8.0,78.1,0.20\n"
    "2026-09-03T12:00:00Z,0.0,8.0,78.2,NaN\n"
)


def test_csv_pivot_shape_ascending_axes_and_nan_survives():
    cols, units, rows = _parse_csv_text(_GRID_CSV)
    assert cols == ["time", "altitude", "latitude", "longitude", "chlor_a"]
    assert len(rows) == 6

    oc = OceanColour()
    lats, lons, arr = oc._pivot_grid(rows, "chlor_a")

    assert arr.shape == (2, 3)
    # Ascending regardless of the 9.0-before-8.0 order the rows arrived in.
    assert list(lats) == [8.0, 9.0]
    assert list(lons) == [78.0, 78.1, 78.2]
    # The NaN cell (lat 8.0, lon 78.2) survives as np.nan, not zero.
    assert np.isnan(arr[0, 2])
    # A handful of real cells landed at the right (lat, lon) position after the flip.
    assert arr[0, 0] == pytest.approx(0.10)   # lat 8.0, lon 78.0
    assert arr[0, 1] == pytest.approx(0.20)   # lat 8.0, lon 78.1
    assert arr[1, 0] == pytest.approx(0.30)   # lat 9.0, lon 78.0
    assert arr[1, 2] == pytest.approx(0.50)   # lat 9.0, lon 78.2


def test_csv_pivot_already_ascending_input_is_left_as_is():
    """An ascending-latitude dataset (OISST) must not get spuriously flipped."""
    rows = [
        {"latitude": "8.0", "longitude": "78.0", "sst": "28.1"},
        {"latitude": "8.0", "longitude": "78.1", "sst": "28.2"},
        {"latitude": "9.0", "longitude": "78.0", "sst": "27.9"},
        {"latitude": "9.0", "longitude": "78.1", "sst": "27.8"},
    ]
    oc = OceanColour()
    lats, lons, arr = oc._pivot_grid(rows, "sst")
    assert list(lats) == [8.0, 9.0]
    assert list(lons) == [78.0, 78.1]
    assert arr[0, 0] == pytest.approx(28.1)
    assert arr[1, 1] == pytest.approx(27.8)


def test_csv_pivot_rejects_a_non_rectangular_row_set():
    """A row count that isn't (distinct lats) x (distinct lons) means the response was
    not the clean grid subset expected — this must fail loudly, not silently truncate."""
    rows = [
        {"latitude": "8.0", "longitude": "78.0", "sst": "28.1"},
        {"latitude": "8.0", "longitude": "78.1", "sst": "28.2"},
        {"latitude": "9.0", "longitude": "78.0", "sst": "27.9"},
        # missing (9.0, 78.1) -- 3 rows can't tile a 2x2 grid
    ]
    oc = OceanColour()
    with pytest.raises(SourceError):
        oc._pivot_grid(rows, "sst")


def test_is_missing_treats_empty_and_nan_as_missing():
    assert _is_missing("") is True
    assert _is_missing(None) is True
    assert _is_missing("NaN") is True
    assert _is_missing("nan") is True
    assert _is_missing("0.30") is False


# --------------------------------------------------------------------------------------
# Cache-key determinism — same arguments twice, same key; no wall-clock instant baked
# in (see docs/DECISIONS.md D11 and test_incois_thredds_key.py for why this matters).
# --------------------------------------------------------------------------------------


def test_cache_key_is_deterministic_for_identical_grid_arguments():
    k1 = _cache_key("nesdisVHNnoaaSNPPnoaa20NRTchlaGapfilledDaily", "chlorophyll_slice", bbox=BBOX, time_bound="(last)")
    k2 = _cache_key("nesdisVHNnoaaSNPPnoaa20NRTchlaGapfilledDaily", "chlorophyll_slice", bbox=BBOX, time_bound="(last)")
    assert k1 == k2


def test_cache_key_is_deterministic_for_identical_point_arguments():
    k1 = _cache_key("ncdcOisst21Agg", "sst_series", lat=9.45, lon=79.3, time_bound="2003-01-01T00:00:00Z:2026-01-01T00:00:00Z")
    k2 = _cache_key("ncdcOisst21Agg", "sst_series", lat=9.45, lon=79.3, time_bound="2003-01-01T00:00:00Z:2026-01-01T00:00:00Z")
    assert k1 == k2


def test_cache_key_distinguishes_real_differences():
    base = _cache_key("ncdcOisst21Agg", "sst_series", lat=9.45, lon=79.3, time_bound="t0:t1")
    diff_dataset = _cache_key("erdMH1chla1day_R2022NRT", "sst_series", lat=9.45, lon=79.3, time_bound="t0:t1")
    diff_op = _cache_key("ncdcOisst21Agg", "chlorophyll_series", lat=9.45, lon=79.3, time_bound="t0:t1")
    diff_point = _cache_key("ncdcOisst21Agg", "sst_series", lat=9.46, lon=79.3, time_bound="t0:t1")
    diff_time = _cache_key("ncdcOisst21Agg", "sst_series", lat=9.45, lon=79.3, time_bound="t0:t2")
    assert len({base, diff_dataset, diff_op, diff_point, diff_time}) == 5


def test_cache_key_never_takes_a_wall_clock_argument():
    """The D11 failure mode: a key must be buildable from request-time arguments alone,
    never from something the function resolves internally (e.g. utcnow())."""
    import inspect

    sig = inspect.signature(_cache_key)
    for forbidden in ("now", "utcnow", "at"):
        assert forbidden not in sig.parameters


# --------------------------------------------------------------------------------------
# Time-range clamping — the private helper sst_series() calls, tested directly per the
# brief ("test the private helper, not the network call").
# --------------------------------------------------------------------------------------

_COVERAGE_START = datetime(1981, 9, 1, tzinfo=UTC)
_COVERAGE_END = datetime(2026, 8, 21, 12, tzinfo=UTC)


def test_clamp_no_op_when_fully_inside_coverage():
    start = datetime(2003, 1, 1, tzinfo=UTC)
    end = datetime(2020, 1, 1, tzinfo=UTC)
    new_start, new_end, clamped = _clamp_time_range(start, end, _COVERAGE_START, _COVERAGE_END)
    assert (new_start, new_end, clamped) == (start, end, False)


def test_clamp_start_before_record():
    start = datetime(1970, 1, 1, tzinfo=UTC)
    end = datetime(1990, 1, 1, tzinfo=UTC)
    new_start, new_end, clamped = _clamp_time_range(start, end, _COVERAGE_START, _COVERAGE_END)
    assert new_start == _COVERAGE_START
    assert new_end == end
    assert clamped is True


def test_clamp_end_past_record():
    start = datetime(2020, 1, 1, tzinfo=UTC)
    end = datetime(2030, 1, 1, tzinfo=UTC)
    new_start, new_end, clamped = _clamp_time_range(start, end, _COVERAGE_START, _COVERAGE_END)
    assert new_start == start
    assert new_end == _COVERAGE_END
    assert clamped is True


def test_clamp_both_bounds_outside_record():
    start = datetime(1970, 1, 1, tzinfo=UTC)
    end = datetime(2030, 1, 1, tzinfo=UTC)
    new_start, new_end, clamped = _clamp_time_range(start, end, _COVERAGE_START, _COVERAGE_END)
    assert (new_start, new_end, clamped) == (_COVERAGE_START, _COVERAGE_END, True)


def test_clamp_is_a_noop_when_no_coverage_bounds_are_known():
    start = datetime(2000, 1, 1, tzinfo=UTC)
    end = datetime(2001, 1, 1, tzinfo=UTC)
    new_start, new_end, clamped = _clamp_time_range(start, end, None, None)
    assert (new_start, new_end, clamped) == (start, end, False)


# --------------------------------------------------------------------------------------
# metadata() unknown-product guard — pure, no network (raises before any fetch).
# --------------------------------------------------------------------------------------


def test_metadata_rejects_unknown_product():
    oc = OceanColour()
    with pytest.raises(SourceError):
        oc.metadata("not_a_real_product")


def test_chlorophyll_slice_rejects_sst_as_a_chlorophyll_product():
    """`sst` is a real DATASETS key but not a legal ChlProduct — chlorophyll_slice must
    say so before ever building a URL, not silently query the wrong grid."""
    oc = OceanColour()
    with pytest.raises(SourceError):
        oc.chlorophyll_slice(BBOX, product="sst")  # type: ignore[arg-type]
