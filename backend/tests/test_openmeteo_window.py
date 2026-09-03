"""Regression test for a fixture-mode determinism bug in ``sources/openmeteo.py``.

Found while manually exercising ``tools/sea_state.py`` against the frozen fixture
snapshot: calling ``get_sea_state`` for "now" (``when=None``) repeatedly, in
``FORESHORE_MODE=fixture``, non-deterministically reported ``openmeteo_marine`` as
missing on roughly half of otherwise-identical calls — a direct violation of CLAUDE.md's
own invariant 7 ("fixture mode replays frozen snapshots... immune to venue wifi"; replay
must be deterministic, or it isn't actually immune to anything).

Root cause: ``_OpenMeteoAdapter.at()`` resolved "now" once (for ``when``) and
``_window_for`` resolved "now" *again*, independently, a few microseconds later. Two
back-to-back ``utcnow()`` calls occasionally return different values (ordinary clock
resolution/scheduling jitter), which flips ``_window_for``'s sign branch and changes the
returned ``(past_hours, forecast_hours)`` — which are themselves part of the outbound
request's query params and therefore part of ``store/cache.py::key_for``'s fixture cache
key. Two logically-identical "give me now" requests could therefore compute two
*different* fixture keys, and only one of them has ever actually been frozen.

The fix: ``_window_for`` takes an explicit ``now`` from its caller instead of querying
the clock itself, and ``.at()`` captures exactly one ``now`` and reuses it as both the
"now" reference and, when the caller passed no ``when``, as ``when`` itself — so
``delta_h`` is exactly ``0.0`` for a "now" request, every time, not a coin flip.
"""

from __future__ import annotations

from datetime import timedelta

from foreshore.models import UTC, utcnow
from foreshore.sources.openmeteo import _window_for


def test_window_for_is_a_pure_function_of_its_two_explicit_arguments():
    """Same (when, now) in -> same (past_hours, forecast_hours) out, always. This is
    the property the original bug violated by sourcing one of the two instants from a
    second, independent clock read instead of from the caller."""
    now = utcnow()
    when = now
    first = _window_for(when, now=now)
    for _ in range(50):
        assert _window_for(when, now=now) == first


def test_now_request_never_races_a_second_internal_clock_read():
    """The specific failure mode: call `_window_for` back-to-back the way `.at()` used
    to (once for `when`, a fresh `utcnow()` a moment later for the delta) and confirm
    that no longer happens -- passing the *same* captured instant for both arguments,
    repeatedly, must never disagree with itself regardless of how much real wall-clock
    time elapses between iterations of this loop."""
    results = set()
    for _ in range(200):
        now = utcnow()
        results.add(_window_for(now, now=now))
    assert len(results) == 1, f"a 'right now' request produced more than one window: {results}"


def test_future_when_still_computes_a_real_forward_window():
    now = utcnow()
    when = now + timedelta(hours=5)
    past_h, fwd_h = _window_for(when, now=now)
    assert past_h == 0
    assert fwd_h >= 6  # ceil(5) + 1


def test_past_when_still_computes_a_real_backward_window():
    now = utcnow()
    when = now - timedelta(hours=3)
    past_h, fwd_h = _window_for(when, now=now)
    assert past_h >= 4  # ceil(3) + 1
    assert fwd_h == 1
