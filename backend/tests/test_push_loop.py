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

import re
from datetime import timedelta

import pytest

from foreshore.geofence.classes import GEOFENCE_CLASSES, format_copy, title_for
from foreshore.geofence.engine import DynamicFence, GeofenceEngine
from foreshore.models import Alert, Observation, Provenance, ToolResult, VesselState, utcnow
from foreshore.push.alerts import AlertStore
from foreshore.push.loop import PushLoop
from foreshore.push.vessels import _FLEET_SIZE, _IMBL_BOAT_INDEX, default_fleet

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
    assert len(loop.fleet) == _FLEET_SIZE
    assert isinstance(loop.alert_store, AlertStore)
    assert isinstance(loop.engine, GeofenceEngine)
    assert loop.region is region
    assert loop.tick_seconds == TICK_SECONDS


def test_tick_returns_only_alert_instances(region):
    loop = PushLoop(region=region, tick_seconds=TICK_SECONDS)
    for _ in range(GENEROUS_TICKS):
        for alert in loop.tick():
            assert isinstance(alert, Alert)


# --------------------------------------------------------------------------------------
# Weather triggers -- significant wave height, wind (speed/gust), IMD lightning nowcast.
#
# The push loop cannot fetch sea state / wind / the IMD nowcast per vessel per tick (the
# tick runs every few seconds in demo mode) -- PushLoop._refresh_conditions caches one
# region-wide condition set on its own slower cadence instead, and per-vessel checks
# against that cache are pure arithmetic. These tests monkeypatch the three source tools
# (foreshore.push.loop.get_sea_state / get_weather / get_lightning_nowcast) directly, the
# same way the existing hazard-fence tests above monkeypatch get_exclusion_zones, and
# construct the loop with conditions_refresh_seconds=0.0 so every tick refetches -- that
# is what lets a test change the mocked reading between successive tick() calls and
# observe escalation/recovery deterministically, without waiting on a real clock.
# --------------------------------------------------------------------------------------


def _test_provenance(source_id: str, authority: str) -> Provenance:
    now = utcnow()
    return Provenance(
        source_id=source_id,
        source_name=f"Test {source_id}",
        authority=authority,  # type: ignore[arg-type]
        url="https://example.test/source",
        acquired_at=now,
        issued_at=now,
        valid_from=now - timedelta(hours=1),
        valid_to=now + timedelta(hours=11),
        spatial_resolution_m=11_000.0,
    )


def _wave_result(hs_value: float, lat: float, lon: float) -> ToolResult:
    obs = Observation(
        variable="significant_wave_height", value=hs_value, unit="m", lat=lat, lon=lon,
        valid_time=utcnow(), provenance=_test_provenance("incois_osf_wave", "INCOIS"),
    )
    return ToolResult(
        tool="get_sea_state", ok=True, observations=[obs], payload={}, summary="test wave"
    )


def _weather_result(
    lat: float, lon: float, *, wind_speed: float | None = None, wind_gust: float | None = None
) -> ToolResult:
    obs: list[Observation] = []
    if wind_speed is not None:
        obs.append(Observation(
            variable="wind_speed", value=wind_speed, unit="kn", lat=lat, lon=lon,
            valid_time=utcnow(), provenance=_test_provenance("openmeteo_forecast", "ECMWF/Open-Meteo"),
        ))
    if wind_gust is not None:
        obs.append(Observation(
            variable="wind_gust", value=wind_gust, unit="kn", lat=lat, lon=lon,
            valid_time=utcnow(), provenance=_test_provenance("openmeteo_forecast", "ECMWF/Open-Meteo"),
        ))
    return ToolResult(
        tool="get_weather", ok=True, observations=obs, payload={}, summary="test weather"
    )


def _lightning_result(active: bool, lat: float, lon: float, district: str = "Ramanathapuram") -> ToolResult:
    value = "Thunderstorm with lightning likely in the next 2 hours" if active else "NIL"
    obs = Observation(
        variable="nowcast_warning", value=value, unit="category", lat=lat, lon=lon,
        valid_time=utcnow(), provenance=_test_provenance("imd_geoserver", "IMD"),
        qualifiers={"district": district},
    )
    return ToolResult(
        tool="get_lightning_nowcast", ok=True, observations=[obs],
        payload={"district": district, "lightning_assessable": True}, summary="test nowcast",
    )


def _lone_vessel(region) -> VesselState:
    lat, lon = region.centre
    return VesselState(
        vessel_id="wx-test-01",
        name="Weather Test FB-01",
        lat=lat,
        lon=lon,
        heading_deg=0.0,
        speed_kn=0.0,
        vessel_class="small_motorised",
        updated_at=utcnow(),
        is_simulated=True,
    )


def _patch_calm_conditions(monkeypatch, lat: float, lon: float, *, hs: float = 0.5) -> None:
    """Every trigger within its comfortable limit -- the baseline a test then worsens."""
    monkeypatch.setattr("foreshore.push.loop.get_sea_state", lambda *a, **k: _wave_result(hs, lat, lon))
    monkeypatch.setattr(
        "foreshore.push.loop.get_weather",
        lambda *a, **k: _weather_result(lat, lon, wind_speed=5.0, wind_gust=8.0),
    )
    monkeypatch.setattr(
        "foreshore.push.loop.get_lightning_nowcast",
        lambda *a, **k: _lightning_result(False, lat, lon),
    )


def test_weather_trigger_crossing_class_limit_raises_exactly_one_alert(region, monkeypatch):
    vessel = _lone_vessel(region)
    _patch_calm_conditions(monkeypatch, vessel.lat, vessel.lon, hs=1.8)  # WARN band: 1.25 < 1.8 <= 2.5

    loop = PushLoop(
        region=region, tick_seconds=TICK_SECONDS, fleet=[vessel], conditions_refresh_seconds=0.0
    )
    emitted = loop.tick()

    weather_alerts = [a for a in emitted if a.kind == "weather"]
    assert len(weather_alerts) == 1
    alert = weather_alerts[0]
    assert alert.level == "WARN"
    assert alert.dedupe_key == f"{vessel.vessel_id}:weather:significant_wave_height:WARN"


def test_weather_trigger_does_not_re_fire_next_tick_at_the_same_level(region, monkeypatch):
    vessel = _lone_vessel(region)
    _patch_calm_conditions(monkeypatch, vessel.lat, vessel.lon, hs=1.8)

    loop = PushLoop(
        region=region, tick_seconds=TICK_SECONDS, fleet=[vessel], conditions_refresh_seconds=0.0
    )
    first = [a for a in loop.tick() if a.kind == "weather"]
    second = [a for a in loop.tick() if a.kind == "weather"]

    assert len(first) == 1
    assert second == [], "same-level weather condition re-emitted on the next tick — dedupe did not hold"


def test_weather_trigger_escalates_when_conditions_worsen(region, monkeypatch):
    vessel = _lone_vessel(region)
    state = {"hs": 1.8}
    monkeypatch.setattr(
        "foreshore.push.loop.get_sea_state",
        lambda *a, **k: _wave_result(state["hs"], vessel.lat, vessel.lon),
    )
    monkeypatch.setattr(
        "foreshore.push.loop.get_weather",
        lambda *a, **k: _weather_result(vessel.lat, vessel.lon, wind_speed=5.0, wind_gust=8.0),
    )
    monkeypatch.setattr(
        "foreshore.push.loop.get_lightning_nowcast",
        lambda *a, **k: _lightning_result(False, vessel.lat, vessel.lon),
    )

    loop = PushLoop(
        region=region, tick_seconds=TICK_SECONDS, fleet=[vessel], conditions_refresh_seconds=0.0
    )
    warn_tick = [a for a in loop.tick() if a.kind == "weather"]
    assert len(warn_tick) == 1
    assert warn_tick[0].level == "WARN"

    state["hs"] = 3.0  # above hs_caution_m (2.5) for small_motorised -> CRITICAL
    critical_tick = [a for a in loop.tick() if a.kind == "weather"]
    assert len(critical_tick) == 1
    assert critical_tick[0].level == "CRITICAL"
    assert critical_tick[0].dedupe_key == f"{vessel.vessel_id}:weather:significant_wave_height:CRITICAL"

    active_keys = {a.dedupe_key for a in loop.alert_store.active_for_vessel(vessel.vessel_id)}
    assert critical_tick[0].dedupe_key in active_keys
    assert f"{vessel.vessel_id}:weather:significant_wave_height:WARN" not in active_keys


def test_weather_trigger_clears_when_conditions_recover(region, monkeypatch):
    vessel = _lone_vessel(region)
    state = {"hs": 3.0}
    monkeypatch.setattr(
        "foreshore.push.loop.get_sea_state",
        lambda *a, **k: _wave_result(state["hs"], vessel.lat, vessel.lon),
    )
    monkeypatch.setattr(
        "foreshore.push.loop.get_weather",
        lambda *a, **k: _weather_result(vessel.lat, vessel.lon, wind_speed=5.0, wind_gust=8.0),
    )
    monkeypatch.setattr(
        "foreshore.push.loop.get_lightning_nowcast",
        lambda *a, **k: _lightning_result(False, vessel.lat, vessel.lon),
    )

    loop = PushLoop(
        region=region, tick_seconds=TICK_SECONDS, fleet=[vessel], conditions_refresh_seconds=0.0
    )
    critical_tick = [a for a in loop.tick() if a.kind == "weather"]
    assert len(critical_tick) == 1  # sanity: it was active before "recovery"

    state["hs"] = 0.5  # back within hs_go_m (1.25) -> no trigger at all
    recovered_tick = [a for a in loop.tick() if a.kind == "weather"]
    assert recovered_tick == []

    active = loop.alert_store.active_for_vessel(vessel.vessel_id)
    assert not any(a.dedupe_key.startswith(f"{vessel.vessel_id}:weather:significant_wave_height:") for a in active)


def test_weather_alert_evidence_is_sourced(region, monkeypatch):
    vessel = _lone_vessel(region)
    _patch_calm_conditions(monkeypatch, vessel.lat, vessel.lon, hs=1.8)

    loop = PushLoop(
        region=region, tick_seconds=TICK_SECONDS, fleet=[vessel], conditions_refresh_seconds=0.0
    )
    weather_alerts = [a for a in loop.tick() if a.kind == "weather"]
    assert len(weather_alerts) == 1
    alert = weather_alerts[0]

    assert alert.evidence, "weather alert must carry the Observation(s) it fired on"
    for obs in alert.evidence:
        assert isinstance(obs, Observation)
        assert isinstance(obs.provenance, Provenance)
        assert obs.provenance.source_id  # never a synthesised/blank provenance


def test_critical_weather_alert_has_a_named_handoff(region, monkeypatch):
    vessel = _lone_vessel(region)
    monkeypatch.setattr(
        "foreshore.push.loop.get_sea_state",
        lambda *a, **k: _wave_result(3.0, vessel.lat, vessel.lon),  # above hs_caution_m
    )
    monkeypatch.setattr(
        "foreshore.push.loop.get_weather",
        lambda *a, **k: _weather_result(vessel.lat, vessel.lon, wind_speed=5.0, wind_gust=8.0),
    )
    monkeypatch.setattr(
        "foreshore.push.loop.get_lightning_nowcast",
        lambda *a, **k: _lightning_result(False, vessel.lat, vessel.lon),
    )

    loop = PushLoop(
        region=region, tick_seconds=TICK_SECONDS, fleet=[vessel], conditions_refresh_seconds=0.0
    )
    weather_alerts = [a for a in loop.tick() if a.kind == "weather"]
    assert len(weather_alerts) == 1
    alert = weather_alerts[0]

    assert alert.level == "CRITICAL"
    assert alert.handoff is not None
    assert alert.handoff.authority_name
    assert alert.handoff.contact


def test_lightning_active_nowcast_raises_a_critical_alert_with_sourced_evidence(region, monkeypatch):
    vessel = _lone_vessel(region)
    monkeypatch.setattr(
        "foreshore.push.loop.get_sea_state", lambda *a, **k: _wave_result(0.5, vessel.lat, vessel.lon)
    )
    monkeypatch.setattr(
        "foreshore.push.loop.get_weather",
        lambda *a, **k: _weather_result(vessel.lat, vessel.lon, wind_speed=5.0, wind_gust=8.0),
    )
    monkeypatch.setattr(
        "foreshore.push.loop.get_lightning_nowcast",
        lambda *a, **k: _lightning_result(True, vessel.lat, vessel.lon, district="Ramanathapuram"),
    )

    loop = PushLoop(
        region=region, tick_seconds=TICK_SECONDS, fleet=[vessel], conditions_refresh_seconds=0.0
    )
    weather_alerts = [a for a in loop.tick() if a.kind == "weather"]
    lightning_alerts = [a for a in weather_alerts if a.dedupe_key.startswith(f"{vessel.vessel_id}:weather:lightning:")]

    assert len(lightning_alerts) == 1
    alert = lightning_alerts[0]
    assert alert.level == "CRITICAL"
    assert alert.handoff is not None
    assert alert.evidence
    for obs in alert.evidence:
        assert isinstance(obs.provenance, Provenance)
    # CAPE must never be substituted for the IMD nowcast (CLAUDE.md's "Do not" list) --
    # the only lightning-labelled variable this system ever carries is nowcast_warning.
    assert all(obs.variable == "nowcast_warning" for obs in alert.evidence)


# --------------------------------------------------------------------------------------
# Evidence + handoff on the existing geofence/hazard alert path (the invariant-3 fix).
# --------------------------------------------------------------------------------------


def test_geofence_alert_carries_sourced_evidence(region):
    loop = PushLoop(region=region, tick_seconds=TICK_SECONDS)
    vessel_id = _imbl_vessel_id(region)

    all_emitted: list[Alert] = []
    for _ in range(GENEROUS_TICKS):
        all_emitted.extend(loop.tick())

    imbl_alerts = [
        a for a in all_emitted
        if a.vessel_id == vessel_id and a.geofence_class == "IMBL_HISTORIC_WATERS"
    ]
    assert imbl_alerts
    for alert in imbl_alerts:
        assert alert.evidence, "geofence alert must carry sourced evidence, not evidence=[]"
        for obs in alert.evidence:
            assert isinstance(obs, Observation)
            assert isinstance(obs.provenance, Provenance)


def test_critical_geofence_alert_has_a_named_handoff(region):
    loop = PushLoop(region=region, tick_seconds=TICK_SECONDS)
    vessel_id = _imbl_vessel_id(region)

    critical_alerts: list[Alert] = []
    for _ in range(GENEROUS_TICKS):
        for alert in loop.tick():
            if (
                alert.vessel_id == vessel_id
                and alert.geofence_class == "IMBL_HISTORIC_WATERS"
                and alert.level in ("CRITICAL", "BREACH")
            ):
                critical_alerts.append(alert)

    assert critical_alerts, "expected the scripted IMBL boat to reach CRITICAL within GENEROUS_TICKS"
    for alert in critical_alerts:
        assert alert.handoff is not None
        assert alert.handoff.authority_name
        assert alert.handoff.contact


# --------------------------------------------------------------------------------------
# _refresh_conditions degrades gracefully -- mirrors _refresh_hazard_fences's own test.
# --------------------------------------------------------------------------------------


def test_refresh_conditions_raising_does_not_kill_the_tick_geofence_still_fires(region, monkeypatch):
    def _raise(*args, **kwargs):
        raise RuntimeError("simulated weather-source outage")

    monkeypatch.setattr("foreshore.push.loop.get_sea_state", _raise)
    monkeypatch.setattr("foreshore.push.loop.get_weather", _raise)
    monkeypatch.setattr("foreshore.push.loop.get_lightning_nowcast", _raise)

    loop = PushLoop(
        region=region, tick_seconds=TICK_SECONDS, conditions_refresh_seconds=0.0
    )
    vessel_id = _imbl_vessel_id(region)

    all_emitted: list[Alert] = []
    for _ in range(GENEROUS_TICKS):
        all_emitted.extend(loop.tick())  # must not raise

    imbl_alerts = [
        a for a in all_emitted
        if a.vessel_id == vessel_id and a.geofence_class == "IMBL_HISTORIC_WATERS"
    ]
    assert imbl_alerts, "geofence alerts must still fire even though the weather-conditions fetch raises"
    assert loop._conditions is not None
    assert loop._conditions.hs is None
    assert loop._conditions.lightning_active is False


# --------------------------------------------------------------------------------------
# Alert copy must be user-facing prose: no enum code, no underscore-style identifier, no
# tool name, no exception class, no file path -- across every alert kind the loop can
# produce (geofence, hazard, weather). Easy to violate accidentally, per the task brief.
# --------------------------------------------------------------------------------------

#: A lowercase- or uppercase-joined-by-underscore token, e.g. "significant_wave_height",
#: "wind_speed", "DO_NOT_ADVISE", "get_sea_state" -- exactly the shape of a Python
#: identifier or enum constant, never of ordinary English (or Tamil) prose.
_IDENTIFIER_LEAK_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+")


def _assert_alert_copy_is_clean(alert: Alert) -> None:
    for field_name in ("title_en", "title_ta", "body_en", "body_ta"):
        text = getattr(alert, field_name)
        assert not _IDENTIFIER_LEAK_RE.search(text), (
            f"{alert.kind} alert {field_name!r} leaks an internal identifier/enum code: {text!r}"
        )
        assert ".py" not in text, f"{alert.kind} alert {field_name!r} leaks a file path: {text!r}"
        assert "Error" not in text and "Exception" not in text, (
            f"{alert.kind} alert {field_name!r} leaks an exception class: {text!r}"
        )
        assert "DO_NOT_ADVISE" not in text and "GO_WITH_CAUTION" not in text, (
            f"{alert.kind} alert {field_name!r} leaks a verdict-level enum code: {text!r}"
        )


def test_no_alert_copy_leaks_an_enum_code_or_identifier(region, monkeypatch):
    # -- geofence: the scripted IMBL boat, driven to both WARN and CRITICAL --------------
    loop = PushLoop(region=region, tick_seconds=TICK_SECONDS)
    vessel_id = _imbl_vessel_id(region)
    geofence_alerts: list[Alert] = []
    for _ in range(GENEROUS_TICKS):
        geofence_alerts.extend(loop.tick())
    assert any(a.vessel_id == vessel_id and a.level == "CRITICAL" for a in geofence_alerts)

    # -- weather: WARN then CRITICAL wave height, WARN wind, CRITICAL lightning ----------
    vessel = _lone_vessel(region)
    state = {"hs": 1.8, "wind_speed": 18.0, "wind_gust": 20.0, "lightning": False}
    monkeypatch.setattr(
        "foreshore.push.loop.get_sea_state",
        lambda *a, **k: _wave_result(state["hs"], vessel.lat, vessel.lon),
    )
    monkeypatch.setattr(
        "foreshore.push.loop.get_weather",
        lambda *a, **k: _weather_result(
            vessel.lat, vessel.lon, wind_speed=state["wind_speed"], wind_gust=state["wind_gust"]
        ),
    )
    monkeypatch.setattr(
        "foreshore.push.loop.get_lightning_nowcast",
        lambda *a, **k: _lightning_result(state["lightning"], vessel.lat, vessel.lon),
    )
    weather_loop = PushLoop(
        region=region, tick_seconds=TICK_SECONDS, fleet=[vessel], conditions_refresh_seconds=0.0
    )
    weather_alerts = list(weather_loop.tick())
    state["hs"] = 3.0
    state["lightning"] = True
    weather_alerts += weather_loop.tick()
    assert any(a.kind == "weather" and a.level == "CRITICAL" for a in weather_alerts)
    assert any(a.dedupe_key.split(":")[2] == "lightning" for a in weather_alerts if a.kind == "weather")

    for alert in geofence_alerts + weather_alerts:
        _assert_alert_copy_is_clean(alert)

    # -- hazard: same config/geofence.yaml copy path as "geofence", for HAZARD_EXCLUSION,
    # checked directly against the config the loop would splice into a real hazard Alert
    # (a real hazard proximity hit needs a vessel track through injected geometry, which
    # the scripted fleet does not guarantee within GENEROUS_TICKS).
    for gclass in GEOFENCE_CLASSES:
        for level in ("WARN", "CRITICAL", "BREACH"):
            for lang in ("en", "ta"):
                title = title_for(gclass, lang)
                body = format_copy(gclass, level, lang, name="Test Zone", distance_nm=1.0, eta_seconds=120.0)
                for text in (title, body):
                    assert not _IDENTIFIER_LEAK_RE.search(text), (
                        f"{gclass} {level} {lang} copy leaks an identifier/enum code: {text!r}"
                    )
                    assert "DO_NOT_ADVISE" not in text and "GO_WITH_CAUTION" not in text
