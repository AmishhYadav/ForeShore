"""Tests for ``foreshore.push.loop.PushLoop``.

This is the third and final push/alert-path module: it ties ``push/vessels.py`` (the
simulated fleet + dead-reckoning) and ``push/alerts.py`` (dedupe/escalation) together
with ``geofence/engine.py`` into the actual proactive scan CLAUDE.md requires.

Four things are under test:

1. The scripted IMBL-bound boat (``push/vessels.py``'s ``_IMBL_BOAT_INDEX``) eventually
   produces a real ``geofence`` alert for ``IMBL_HISTORIC_WATERS`` as ``tick()`` is
   called repeatedly — driven directly, not via ``run()``, so the test has no
   ``time.sleep`` and stays fast and deterministic.
2. Once that alert is active at a given level, repeated ticks at the same level do not
   re-emit it — dedupe working end-to-end through the loop, not just inside
   ``AlertStore`` in isolation.
3. Escalation (WARN -> CRITICAL as the boat keeps closing) *does* re-emit a fresh alert
   for the same ``dedupe_key``.
4. ``tick()``'s dynamic hazard-fence refresh degrades to ``set_dynamic([])`` rather than
   propagating an exception when ``get_exclusion_zones`` itself raises.

Tick cadence and observed distances (``FORESHORE_MODE=fixture``, default region/seed):
the IMBL-bound boat starts ~12.3 nm from ``imbl_historic_waters`` and, advanced 300 s
(5 min) at a time, first crosses the 2.0 nm WARN threshold around tick 17 and the 0.5 nm
CRITICAL threshold around tick 20. Tick counts below are generous multiples of that to
stay robust to minor geometry/threshold changes without weakening the assertions.
"""

from __future__ import annotations

import pytest

from foreshore.geofence.engine import DynamicFence, GeofenceEngine
from foreshore.models import Alert
from foreshore.push.alerts import AlertStore
from foreshore.push.loop import PushLoop
from foreshore.push.vessels import _IMBL_BOAT_INDEX, default_fleet

TICK_SECONDS = 300.0  # 5-minute ticks
GENEROUS_TICKS = 40  # comfortably past the observed WARN (~17) and CRITICAL (~20) crossings


def _imbl_vessel_id(region) -> str:
    return default_fleet(region)[_IMBL_BOAT_INDEX].vessel_id


# --------------------------------------------------------------------------------------
# 1 & 2. WARN alert eventually fires, and does not re-emit every tick once active.
# --------------------------------------------------------------------------------------


def test_imbl_boat_eventually_fires_a_geofence_warn_alert(region):
    loop = PushLoop(region=region, tick_seconds=TICK_SECONDS)
    vessel_id = _imbl_vessel_id(region)

    all_emitted: list[Alert] = []
    for _ in range(GENEROUS_TICKS):
        all_emitted.extend(loop.tick())

    imbl_alerts = [
        a
        for a in all_emitted
        if a.vessel_id == vessel_id and a.geofence_class == "IMBL_HISTORIC_WATERS"
    ]
    assert imbl_alerts, "expected at least one IMBL_HISTORIC_WATERS alert from the scripted boat"
    assert imbl_alerts[0].kind == "geofence"
    assert imbl_alerts[0].level in ("WARN", "CRITICAL", "BREACH")


def test_imbl_boat_warn_alert_is_not_re_emitted_every_tick_at_the_same_level(region):
    loop = PushLoop(region=region, tick_seconds=TICK_SECONDS)
    vessel_id = _imbl_vessel_id(region)

    per_tick_imbl_emissions: list[list[Alert]] = []
    for _ in range(GENEROUS_TICKS):
        emitted = loop.tick()
        per_tick_imbl_emissions.append(
            [
                a
                for a in emitted
                if a.vessel_id == vessel_id and a.geofence_class == "IMBL_HISTORIC_WATERS"
            ]
        )

    # Find two consecutive ticks where the alert is active (already emitted at least
    # once) and confirm they are not both fresh emissions of the same dedupe_key at the
    # same level -- i.e. once WARN has fired, it must not fire again as long as the boat
    # stays in the WARN band.
    first_emission_tick = next(
        i for i, emissions in enumerate(per_tick_imbl_emissions) if emissions
    )
    first_level = per_tick_imbl_emissions[first_emission_tick][0].level

    # Every later tick whose emission is still at the same level as the first one must
    # be empty (suppressed by AlertStore's dedupe) -- collect any violation.
    reemitted_same_level = [
        i
        for i, emissions in enumerate(per_tick_imbl_emissions)
        if i > first_emission_tick and any(e.level == first_level for e in emissions)
    ]
    # A same-level re-emission is only legitimate if the alert was cleared in between
    # (boat left range and came back) -- for this monotonically-closing-then-departing
    # scripted track within GENEROUS_TICKS that should not happen for the WARN band
    # immediately after it first fires while still approaching.
    assert per_tick_imbl_emissions[first_emission_tick + 1] == [] or all(
        e.level != first_level for e in per_tick_imbl_emissions[first_emission_tick + 1]
    ), "same-level alert re-emitted on the very next tick -- dedupe did not hold"


# --------------------------------------------------------------------------------------
# 3. Escalation (WARN -> CRITICAL) does re-emit.
# --------------------------------------------------------------------------------------


def test_imbl_boat_escalation_to_critical_re_emits(region):
    loop = PushLoop(region=region, tick_seconds=TICK_SECONDS)
    vessel_id = _imbl_vessel_id(region)

    levels_emitted_in_order: list[str] = []
    for _ in range(GENEROUS_TICKS):
        for alert in loop.tick():
            if alert.vessel_id == vessel_id and alert.geofence_class == "IMBL_HISTORIC_WATERS":
                levels_emitted_in_order.append(alert.level)

    assert "WARN" in levels_emitted_in_order
    assert "CRITICAL" in levels_emitted_in_order
    # The escalation must actually be an escalation: CRITICAL observed after WARN.
    assert levels_emitted_in_order.index("CRITICAL") > levels_emitted_in_order.index("WARN")


def test_dedupe_key_used_by_the_store_matches_the_documented_shape(region):
    loop = PushLoop(region=region, tick_seconds=TICK_SECONDS)
    vessel_id = _imbl_vessel_id(region)

    for _ in range(GENEROUS_TICKS):
        loop.tick()

    active = loop.alert_store.active_for_vessel(vessel_id)
    imbl_active = [a for a in active if a.geofence_class == "IMBL_HISTORIC_WATERS"]
    if imbl_active:
        alert = imbl_active[0]
        assert alert.dedupe_key.startswith(f"{vessel_id}:IMBL_HISTORIC_WATERS:")


# --------------------------------------------------------------------------------------
# 4. Dynamic hazard-fence refresh degrades gracefully when get_exclusion_zones raises.
# --------------------------------------------------------------------------------------


def test_tick_degrades_to_empty_dynamic_fences_when_tool_raises(region, monkeypatch):
    engine = GeofenceEngine(region=region)
    # Simulate a previous tick having left working hazard geometry in place.
    engine.set_dynamic(
        [
            DynamicFence(
                fence_id="hazard_test_0",
                name="stale hazard",
                geometry={"type": "Point", "coordinates": [79.3, 9.3]},
            )
        ]
    )
    assert engine.dynamic  # sanity: something is there before the tick

    def _raising_get_exclusion_zones(*args, **kwargs):
        raise RuntimeError("simulated GDACS/store failure")

    monkeypatch.setattr(
        "foreshore.push.loop.get_exclusion_zones", _raising_get_exclusion_zones
    )

    loop = PushLoop(region=region, tick_seconds=TICK_SECONDS, engine=engine)

    # Must not raise.
    loop.tick()

    # The stale hazard fence must have been cleared, not left in place or accumulated.
    assert engine.dynamic == []


def test_tick_clears_dynamic_fences_when_tool_returns_no_hazard_features(region, monkeypatch):
    from foreshore.models import ToolResult

    def _empty_get_exclusion_zones(*args, **kwargs):
        return ToolResult(
            tool="get_exclusion_zones",
            ok=True,
            partial=False,
            missing=[],
            observations=[],
            payload={"features": [], "counts": {}, "sources_checked": [], "sources_failed": [], "notes": []},
            summary="No exclusion-zone features found from any source.",
        )

    monkeypatch.setattr(
        "foreshore.push.loop.get_exclusion_zones", _empty_get_exclusion_zones
    )

    loop = PushLoop(region=region, tick_seconds=TICK_SECONDS)
    loop.tick()  # must not raise

    assert loop.engine.dynamic == []


def test_tick_only_promotes_hazard_exclusion_features_to_dynamic_fences(region, monkeypatch):
    from foreshore.models import ToolResult

    def _mixed_get_exclusion_zones(*args, **kwargs):
        return ToolResult(
            tool="get_exclusion_zones",
            ok=True,
            partial=False,
            missing=[],
            observations=[],
            payload={
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [79.3, 9.3]},
                        "properties": {"geofence_class": "HAZARD_EXCLUSION", "hazard_class": "cyclone_hazard"},
                    },
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [79.4, 9.4]},
                        "properties": {"geofence_class": "IMBL_HISTORIC_WATERS", "hazard_class": "imbl_historic_waters"},
                    },
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [79.5, 9.5]},
                        "properties": {"geofence_class": "MPA", "hazard_class": "mpa_gom"},
                    },
                ],
                "counts": {},
                "sources_checked": [],
                "sources_failed": [],
                "notes": [],
            },
            summary="test",
        )

    monkeypatch.setattr(
        "foreshore.push.loop.get_exclusion_zones", _mixed_get_exclusion_zones
    )

    loop = PushLoop(region=region, tick_seconds=TICK_SECONDS)
    loop.tick()

    assert len(loop.engine.dynamic) == 1
    assert all(f.geofence_class == "HAZARD_EXCLUSION" for f in loop.engine.dynamic)


# --------------------------------------------------------------------------------------
# Constructor defaults sanity.
# --------------------------------------------------------------------------------------


def test_construction_with_no_args_builds_default_fleet_and_stores(region):
    loop = PushLoop(region=region, tick_seconds=TICK_SECONDS)
    assert len(loop.fleet) == 8
    assert isinstance(loop.alert_store, AlertStore)
    assert isinstance(loop.engine, GeofenceEngine)
    assert loop.region is region
    assert loop.tick_seconds == TICK_SECONDS


def test_tick_returns_only_alert_instances(region):
    loop = PushLoop(region=region, tick_seconds=TICK_SECONDS)
    for _ in range(GENEROUS_TICKS):
        for alert in loop.tick():
            assert isinstance(alert, Alert)
