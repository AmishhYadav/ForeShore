"""Tests for ``foreshore.push.alerts.AlertStore``.

This governs whether the push-path demo (and a real deployment) spams alerts or stays
legible: a repeated observation at the same level must not re-emit, an escalation must
always re-emit, and acknowledging or clearing an alert must let a fresh occurrence fire
again. Ordering (``all_active``) must be worst-first so a console renders the most
dangerous thing at the top.
"""

from __future__ import annotations

from datetime import timedelta

from foreshore.geofence.classes import ALERT_RANK
from foreshore.models import Alert, utcnow
from foreshore.push.alerts import AlertStore


def _make_alert(
    *,
    alert_id: str = "a1",
    vessel_id: str = "sim-00",
    level: str = "WARN",
    dedupe_key: str = "sim-00:imbl_historic_waters",
    created_at=None,
    distance_nm: float = 1.5,
) -> Alert:
    return Alert(
        alert_id=alert_id,
        vessel_id=vessel_id,
        kind="geofence",
        level=level,  # type: ignore[arg-type]
        title_en="Approaching IMBL historic-waters line",
        title_ta="IMBL வரலாற்று நீர் எல்லையை நெருங்குகிறது",
        body_en="You are approaching the IMBL historic-waters line.",
        body_ta="நீங்கள் IMBL வரலாற்று நீர் எல்லையை நெருங்குகிறீர்கள்.",
        lat=9.3,
        lon=79.4,
        created_at=created_at or utcnow(),
        dedupe_key=dedupe_key,
        distance_nm=distance_nm,
    )


# --------------------------------------------------------------------------------------
# upsert() semantics
# --------------------------------------------------------------------------------------


def test_upsert_new_alert_is_emitted():
    store = AlertStore()
    alert = _make_alert()

    result = store.upsert(alert)

    assert result is alert
    assert store.active_for_vessel("sim-00") == [alert]


def test_upsert_identical_level_duplicate_is_suppressed_but_refreshes_fields():
    store = AlertStore()
    first = _make_alert(distance_nm=1.8)
    store.upsert(first)

    second = _make_alert(alert_id="a2", distance_nm=1.2)  # same dedupe_key, same level
    result = store.upsert(second)

    assert result is None
    active = store.active_for_vessel("sim-00")
    assert len(active) == 1
    assert active[0].distance_nm == 1.2
    assert active[0].alert_id == "a2"


def test_upsert_escalation_always_re_emits():
    store = AlertStore()
    warn = _make_alert(alert_id="a1", level="WARN")
    store.upsert(warn)

    critical = _make_alert(alert_id="a2", level="CRITICAL")
    result = store.upsert(critical)

    assert result is critical
    active = store.active_for_vessel("sim-00")
    assert len(active) == 1
    assert active[0].level == "CRITICAL"


def test_upsert_downgrade_is_suppressed_not_re_emitted():
    store = AlertStore()
    critical = _make_alert(alert_id="a1", level="CRITICAL")
    store.upsert(critical)

    warn = _make_alert(alert_id="a2", level="WARN")
    result = store.upsert(warn)

    assert result is None
    active = store.active_for_vessel("sim-00")
    assert len(active) == 1
    # Stored record still reflects the (lower-ranked) latest observation's fields.
    assert active[0].level == "WARN"


def test_upsert_distinct_dedupe_keys_are_independent():
    store = AlertStore()
    a = _make_alert(alert_id="a1", dedupe_key="sim-00:imbl_historic_waters")
    b = _make_alert(alert_id="a2", dedupe_key="sim-00:mpa_gom", vessel_id="sim-00")

    assert store.upsert(a) is a
    assert store.upsert(b) is b
    assert len(store.active_for_vessel("sim-00")) == 2


# --------------------------------------------------------------------------------------
# acknowledge()
# --------------------------------------------------------------------------------------


def test_acknowledge_removes_from_active_and_returns_the_alert():
    store = AlertStore()
    alert = _make_alert()
    store.upsert(alert)

    acked = store.acknowledge("a1", by="skipper")

    assert acked is not None
    assert acked.alert_id == "a1"
    assert acked.acknowledged_by == "skipper"
    assert acked.acknowledged_at is not None
    assert store.active_for_vessel("sim-00") == []


def test_acknowledge_unknown_id_returns_none():
    store = AlertStore()
    assert store.acknowledge("does-not-exist", by="skipper") is None


def test_acknowledge_defaults_when_to_now():
    store = AlertStore()
    store.upsert(_make_alert())
    before = utcnow()

    acked = store.acknowledge("a1", by="skipper")

    after = utcnow()
    assert before <= acked.acknowledged_at <= after


def test_acknowledge_accepts_explicit_when():
    store = AlertStore()
    store.upsert(_make_alert())
    when = utcnow() - timedelta(hours=1)

    acked = store.acknowledge("a1", by="skipper", when=when)

    assert acked.acknowledged_at == when


def test_fresh_occurrence_after_acknowledge_fires_again_even_at_same_level():
    store = AlertStore()
    store.upsert(_make_alert(alert_id="a1", level="WARN"))
    store.acknowledge("a1", by="skipper")

    fresh = _make_alert(alert_id="a3", level="WARN")
    result = store.upsert(fresh)

    assert result is fresh
    assert store.active_for_vessel("sim-00") == [fresh]


# --------------------------------------------------------------------------------------
# clear()
# --------------------------------------------------------------------------------------


def test_clear_removes_without_acknowledging():
    store = AlertStore()
    alert = _make_alert()
    store.upsert(alert)

    store.clear(alert.dedupe_key)

    assert store.active_for_vessel("sim-00") == []
    assert alert.acknowledged_at is None
    assert alert.acknowledged_by is None


def test_fresh_occurrence_after_clear_fires_again():
    store = AlertStore()
    store.upsert(_make_alert(alert_id="a1", level="WARN"))
    store.clear("sim-00:imbl_historic_waters")

    fresh = _make_alert(alert_id="a4", level="WARN")
    result = store.upsert(fresh)

    assert result is fresh


def test_clear_unknown_key_is_a_no_op():
    store = AlertStore()
    store.clear("nothing:here")  # must not raise


# --------------------------------------------------------------------------------------
# all_active() ordering
# --------------------------------------------------------------------------------------


def test_all_active_orders_worst_first():
    store = AlertStore()
    now = utcnow()

    info = _make_alert(alert_id="a1", level="INFO", dedupe_key="k1", created_at=now)
    warn = _make_alert(alert_id="a2", level="WARN", dedupe_key="k2", created_at=now)
    critical = _make_alert(alert_id="a3", level="CRITICAL", dedupe_key="k3", created_at=now)
    breach = _make_alert(alert_id="a4", level="BREACH", dedupe_key="k4", created_at=now)

    for a in (info, warn, critical, breach):
        store.upsert(a)

    ordered = store.all_active()
    levels = [a.level for a in ordered]
    assert levels == ["BREACH", "CRITICAL", "WARN", "INFO"]
    ranks = [ALERT_RANK[lvl] for lvl in levels]
    assert ranks == sorted(ranks, reverse=True)


def test_all_active_orders_oldest_first_within_same_level():
    store = AlertStore()
    t0 = utcnow()
    t1 = t0 + timedelta(seconds=30)

    older = _make_alert(alert_id="a1", level="WARN", dedupe_key="k1", created_at=t0)
    newer = _make_alert(alert_id="a2", level="WARN", dedupe_key="k2", created_at=t1)

    store.upsert(newer)
    store.upsert(older)

    ordered = store.all_active()
    assert [a.alert_id for a in ordered] == ["a1", "a2"]


def test_all_active_empty_store_is_empty_list():
    store = AlertStore()
    assert store.all_active() == []
