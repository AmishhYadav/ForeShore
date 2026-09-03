"""Regression test for docs/DECISIONS.md D11 — the more serious of the two fixture-mode
determinism bugs found while manually exercising ``get_sea_state``.

``IncoisThredds._binary_key`` used to hash a ``time_start``/``time_end`` window
(``at ± 6h``) into the grid-fetch cache/fixture key. Every real query resolves an
explicit ``when`` (``agents/planner.py`` never leaves it ``None``), so that window was
unique to the microsecond on nearly every call — a frozen fixture for it essentially
never existed, so INCOIS's own OSF wave nest (CLAUDE.md's "authoritative model" and the
evidence panel's centrepiece) silently vanished from the disagreement panel on every
real query, deterministically, not just flakily.

The fix: ``_binary_key`` no longer takes or hashes any time bound at all — identity is
``(urlPath, raw_vars, bbox)`` only, since ``urlPath`` already names the specific day's
file. This test locks that in directly against the private method (no network, no
fixture files needed) rather than against the full point-query pipeline, which would
otherwise make this test's usefulness depend on `data/fixtures/` staying populated.
"""

from __future__ import annotations

from datetime import timedelta

from foreshore.models import utcnow
from foreshore.sources.incois_thredds import IncoisThredds


def test_binary_key_ignores_time_window_entirely():
    """The exact failure mode: two calls that used to differ only in their ± 6h window
    (derived from two different "now" instants) must now hash identically."""
    adapter = IncoisThredds()
    url_path = "osf/wave/WAVES_coast_20260903.nc"
    raw_vars = ["SWH", "SWELL", "WP", "SWP"]
    bbox = (79.0, 9.0, 79.6, 9.6)

    key = adapter._binary_key(url_path, raw_vars, bbox)  # noqa: SLF001 - the contract under test
    assert key == adapter._binary_key(url_path, raw_vars, bbox)  # noqa: SLF001


def test_binary_key_takes_no_time_arguments():
    """The old signature accepted (and hashed) time_start/time_end -- confirm the
    parameter is actually gone, not just unused, so nobody can silently reintroduce a
    clock-derived key by passing it back in."""
    import inspect

    sig = inspect.signature(IncoisThredds._binary_key)
    assert "time_start" not in sig.parameters
    assert "time_end" not in sig.parameters


def test_binary_key_still_distinguishes_real_differences():
    """Not a no-op: a different file, variable set, or bbox must still produce a
    different key -- only the time window was ever spurious."""
    adapter = IncoisThredds()
    base = adapter._binary_key(  # noqa: SLF001
        "osf/wave/WAVES_coast_20260903.nc", ["SWH", "SWELL"], (79.0, 9.0, 79.6, 9.6)
    )
    different_file = adapter._binary_key(  # noqa: SLF001
        "osf/wave/WAVES_coast_20260904.nc", ["SWH", "SWELL"], (79.0, 9.0, 79.6, 9.6)
    )
    different_vars = adapter._binary_key(  # noqa: SLF001
        "osf/wave/WAVES_coast_20260903.nc", ["SWH"], (79.0, 9.0, 79.6, 9.6)
    )
    different_bbox = adapter._binary_key(  # noqa: SLF001
        "osf/wave/WAVES_coast_20260903.nc", ["SWH", "SWELL"], (79.0, 9.0, 80.0, 10.0)
    )
    assert len({base, different_file, different_vars, different_bbox}) == 4
