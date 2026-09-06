"""Pure-logic tests for :class:`IncoisOceansat` (ISRO Oceansat-2 OCM, INCOIS ERDDAP).

Runs entirely under ``FORESHORE_MODE=fixture`` (forced session-wide by
``conftest.py``) and opens no socket: no fixture files exist for this dataset, so every
test here either exercises a pure function directly or hand-builds a ``FetchResult`` in
memory rather than going through ``Source.get``.

Four things this file guards, matching the four acceptance bullets in the brief:

1. ``_csv_url`` was generalised (added a ``dataset`` parameter) to serve both
   ``IncoisArgo`` and this class instead of being duplicated -- a regression there would
   silently break every existing Argo query, so the old URL shape is pinned byte-for-byte.
2. ``_clamp_to_coverage`` -- this archive is closed (2011-02-02..2020-05-01) and a
   request outside it must be clamped, flagged, and never presented as current.
3. CSV row parsing -- ``NaN`` rows skipped, unit normalised to ``"mg/m^3"``, and the
   *echoed grid cell* (not the requested point) carried in qualifiers, mirroring the
   nearest-neighbour-snap behaviour ``incois_erddap.py``'s own docstring documents for
   ``IncoisArgo``.
4. ``_series_key`` determinism -- see ``test_incois_thredds_key.py`` for why a
   wall-clock-derived key is a real, previously-shipped bug (it silently dropped a whole
   source from the evidence panel on every live query).
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta

from foreshore.models import UTC
from foreshore.sources.base import FetchResult
from foreshore.sources.incois_erddap import (
    DATASET,
    DATASET_OCEANSAT,
    OCEANSAT_COVERAGE_END,
    OCEANSAT_COVERAGE_START,
    IncoisOceansat,
    _clamp_to_coverage,
    _csv_url,
)


# ----------------------------------------------------------------------------------------
# 1. _csv_url generalisation: new dataset works, old (Argo) call site is unchanged.
# ----------------------------------------------------------------------------------------


def test_csv_url_oceansat_is_correctly_percent_encoded():
    dims_expr = "[(2011-02-02T00:00:00Z):(2020-05-01T00:00:00Z)][(9.45)][(79.3)]"
    url = _csv_url(DATASET_OCEANSAT, ["CHL"], dims_expr)

    assert url.startswith(
        "https://erddap.incois.gov.in/erddap/griddap/incois_oceansat2_datasets.csv?"
    )
    # The raw constraint characters must never appear unescaped in the request line --
    # this is the exact Tomcat 400 this module's docstring documents.
    for raw_char in "[]()":
        assert raw_char not in url
    assert "CHL%5B" in url  # "CHL[" percent-encoded
    assert "%289.45%29" in url  # "(9.45)" percent-encoded


def test_csv_url_argo_unchanged_by_the_refactor():
    """Guards the refactor: the exact same dims_expr through the exact same dataset must
    still produce the exact same URL it did before ``_csv_url`` gained a dataset param."""
    dims_expr = "[(last)][(5):(2000)][(9.2876)][(79.3129)]"
    variables = ["TEMP", "TERR", "SAL", "SERR"]

    url = _csv_url(DATASET, variables, dims_expr)
    expected = (
        "https://erddap.incois.gov.in/erddap/griddap/incois_argo_10d_VAM.csv?"
        + "%2C".join(
            f"{v}%5B%28last%29%5D%5B%285%29%3A%282000%29%5D%5B%289.2876%29%5D%5B%2879.3129%29%5D"
            for v in variables
        )
    )
    assert url == expected


# ----------------------------------------------------------------------------------------
# 2. Time clamping against the closed archive.
# ----------------------------------------------------------------------------------------


def test_clamp_wide_request_to_archive_bounds():
    start = datetime(2003, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 1, tzinfo=UTC)

    clamped_start, clamped_end, was_clamped = _clamp_to_coverage(
        start, end, OCEANSAT_COVERAGE_START, OCEANSAT_COVERAGE_END
    )

    assert clamped_start == OCEANSAT_COVERAGE_START
    assert clamped_end == OCEANSAT_COVERAGE_END
    assert was_clamped is True


def test_clamp_request_wholly_inside_coverage_is_not_flagged():
    start = datetime(2015, 1, 1, tzinfo=UTC)
    end = datetime(2016, 1, 1, tzinfo=UTC)

    clamped_start, clamped_end, was_clamped = _clamp_to_coverage(
        start, end, OCEANSAT_COVERAGE_START, OCEANSAT_COVERAGE_END
    )

    assert clamped_start == start
    assert clamped_end == end
    assert was_clamped is False


def test_clamp_request_entirely_after_coverage_collapses_to_last_instant():
    start = datetime(2022, 1, 1, tzinfo=UTC)
    end = datetime(2023, 1, 1, tzinfo=UTC)

    clamped_start, clamped_end, was_clamped = _clamp_to_coverage(
        start, end, OCEANSAT_COVERAGE_START, OCEANSAT_COVERAGE_END
    )

    assert clamped_start == OCEANSAT_COVERAGE_END
    assert clamped_end == OCEANSAT_COVERAGE_END
    assert was_clamped is True
    assert clamped_start <= clamped_end  # never an inverted range


def test_clamp_request_entirely_before_coverage_collapses_to_first_instant():
    start = datetime(2005, 1, 1, tzinfo=UTC)
    end = datetime(2006, 1, 1, tzinfo=UTC)

    clamped_start, clamped_end, was_clamped = _clamp_to_coverage(
        start, end, OCEANSAT_COVERAGE_START, OCEANSAT_COVERAGE_END
    )

    assert clamped_start == OCEANSAT_COVERAGE_START
    assert clamped_end == OCEANSAT_COVERAGE_START
    assert was_clamped is True


def test_clamp_accepts_naive_datetimes_as_utc():
    """`start`/`end` may arrive naive (a caller that forgot tzinfo); treated as UTC,
    matching `_iso_z`'s own convention, rather than raising a naive/aware TypeError."""
    start = datetime(2003, 1, 1)
    end = datetime(2026, 1, 1)

    clamped_start, clamped_end, was_clamped = _clamp_to_coverage(
        start, end, OCEANSAT_COVERAGE_START, OCEANSAT_COVERAGE_END
    )

    assert was_clamped is True
    assert clamped_start.tzinfo is not None


# ----------------------------------------------------------------------------------------
# 3. CSV row parsing: NaN skipped, unit normalised, echoed grid cell in qualifiers.
# ----------------------------------------------------------------------------------------

_HAND_WRITTEN_CSV = (
    "time,latitude,longitude,CHL\n"
    "UTC,degrees_north,degrees_east,mg m-3\n"
    "2011-02-02T00:00:00Z,9.459555227944358,79.319249520296,0.328\n"
    "2011-02-12T00:00:00Z,9.459555227944358,79.319249520296,NaN\n"
    "2011-02-22T00:00:00Z,9.459555227944358,79.319249520296,0.512\n"
    "2011-03-04T00:00:00Z,9.459555227944358,79.319249520296,1.716\n"
)


def _fake_fetch_result() -> FetchResult:
    return FetchResult(
        payload=_HAND_WRITTEN_CSV,
        url=(
            "https://erddap.incois.gov.in/erddap/griddap/incois_oceansat2_datasets.csv"
            "?CHL%5B%282011-02-02%29%3A%282020-05-01%29%5D%5B%289.45%29%5D%5B%2879.3%29%5D"
        ),
        key="incois_oceansat2_datasets:chl:9.45:79.3:test",
        acquired_at=datetime(2026, 9, 6, tzinfo=UTC),
        status=200,
    )


def test_csv_row_parsing_skips_nan_and_normalises_unit_and_grid_cell():
    adapter = IncoisOceansat()
    raw = _fake_fetch_result()

    observations = adapter._rows_to_observations(  # noqa: SLF001 - the contract under test
        raw,
        requested_lat=9.45,
        requested_lon=79.30,
        resolution_m=4_320.0,
        time_range_clamped=False,
        note="test fixture",
    )

    assert len(observations) == 3  # the 4th data row (NaN) is skipped
    for obs in observations:
        assert obs.variable == "chlorophyll_a"
        assert obs.unit == "mg/m^3"
        # the echoed grid cell, not the requested point, is what's carried
        assert obs.qualifiers["grid_lat"] == 9.459555227944358
        assert obs.qualifiers["grid_lon"] == 79.319249520296
        assert obs.qualifiers["requested_lat"] == 9.45
        assert obs.qualifiers["requested_lon"] == 79.30
        assert obs.lat == 9.459555227944358
        assert obs.lon == 79.319249520296
        assert obs.provenance.spatial_resolution_m == 4_320.0
        assert obs.provenance.authority == "ISRO/NRSC"
        assert obs.provenance.source_id == "incois_oceansat2"

    values = sorted(o.numeric for o in observations)
    assert values == [0.328, 0.512, 1.716]


def test_csv_row_parsing_flags_time_range_clamped_on_every_row():
    adapter = IncoisOceansat()
    raw = _fake_fetch_result()

    observations = adapter._rows_to_observations(  # noqa: SLF001
        raw,
        requested_lat=9.45,
        requested_lon=79.30,
        resolution_m=4_320.0,
        time_range_clamped=True,
        note="test fixture, clamped",
    )

    assert len(observations) == 3
    assert all(o.qualifiers["time_range_clamped"] is True for o in observations)


def test_csv_row_parsing_empty_rows_returns_empty_list():
    adapter = IncoisOceansat()
    raw = FetchResult(
        payload="time,latitude,longitude,CHL\nUTC,degrees_north,degrees_east,mg m-3\n",
        url="https://erddap.incois.gov.in/erddap/griddap/incois_oceansat2_datasets.csv",
        key="empty",
        acquired_at=datetime(2026, 9, 6, tzinfo=UTC),
        status=200,
    )
    observations = adapter._rows_to_observations(  # noqa: SLF001
        raw, requested_lat=9.45, requested_lon=79.30, resolution_m=4_320.0,
        time_range_clamped=False, note="test",
    )
    assert observations == []


# ----------------------------------------------------------------------------------------
# 4. Cache-key determinism -- see test_incois_thredds_key.py for why this matters.
# ----------------------------------------------------------------------------------------


def test_series_key_is_deterministic_for_identical_arguments():
    adapter = IncoisOceansat()
    start = OCEANSAT_COVERAGE_START
    end = OCEANSAT_COVERAGE_START + timedelta(days=365)

    key_a = adapter._series_key(9.45, 79.30, start, end)  # noqa: SLF001
    key_b = adapter._series_key(9.45, 79.30, start, end)  # noqa: SLF001

    assert key_a == key_b


def test_series_key_carries_no_wall_clock_instant():
    """The exact failure mode `test_incois_thredds_key.py` documents: a key must not be
    derived from ``utcnow()``/``datetime.now()`` at call time, or a frozen fixture for it
    would essentially never match on replay. Confirmed two ways: the method's own
    signature takes no "now"-shaped parameter, and two calls separated by real wall-clock
    time (not just two calls in a row) still agree."""
    sig = inspect.signature(IncoisOceansat._series_key)
    param_names = set(sig.parameters) - {"self"}
    assert param_names == {"lat", "lon", "start", "end"}

    adapter = IncoisOceansat()
    start = OCEANSAT_COVERAGE_START
    end = OCEANSAT_COVERAGE_END

    key_before = adapter._series_key(9.45, 79.30, start, end)  # noqa: SLF001
    import time as _time

    _time.sleep(0.01)
    key_after = adapter._series_key(9.45, 79.30, start, end)  # noqa: SLF001
    assert key_before == key_after


def test_series_key_still_distinguishes_real_differences():
    adapter = IncoisOceansat()
    base = adapter._series_key(  # noqa: SLF001
        9.45, 79.30, OCEANSAT_COVERAGE_START, OCEANSAT_COVERAGE_END
    )
    different_lat = adapter._series_key(  # noqa: SLF001
        9.50, 79.30, OCEANSAT_COVERAGE_START, OCEANSAT_COVERAGE_END
    )
    different_lon = adapter._series_key(  # noqa: SLF001
        9.45, 79.35, OCEANSAT_COVERAGE_START, OCEANSAT_COVERAGE_END
    )
    different_start = adapter._series_key(  # noqa: SLF001
        9.45, 79.30, OCEANSAT_COVERAGE_START + timedelta(days=1), OCEANSAT_COVERAGE_END
    )
    assert len({base, different_lat, different_lon, different_start}) == 4
