"""The push/alert loop — FORESHORE's proactive path.

CLAUDE.md is explicit that a request-response-only system fails the problem statement:
the push path (a background scan over tracked vessel positions, firing hazard and
geofence-approach alerts *before* a fisherman asks) is a scored requirement, not a nice
extra. This module is where that scan actually happens.

Each :meth:`PushLoop.tick` does five things, in order, every time it runs:

1. Dead-reckon every tracked vessel forward one tick (:func:`foreshore.push.vessels.advance`
   — pure, deterministic, no network).
2. Refresh the engine's dynamic hazard fences **once per tick, not once per vessel** —
   GDACS cyclone geometry does not change vessel-to-vessel, so re-fetching it per boat
   would be eight times the work for the same answer. Only ``HAZARD_EXCLUSION``-classed
   features from ``get_exclusion_zones`` become dynamic fences; the static IMBL/MPA/eco
   layers are already checked natively by :class:`~foreshore.geofence.engine.GeofenceEngine`
   against the vector store, so re-adding them here would double-count the same boundary
   as two separate hits under two different mechanisms.
2b. Refresh the cached region weather picture (sea state / wind / IMD lightning nowcast)
   — also once per tick call, but internally gated to its own much slower cadence
   (``_refresh_conditions``, default 600 s) — the tick itself runs every few seconds in
   demo mode, and fetching weather per vessel per tick at that cadence is not viable.
   Every vessel's threshold check against that cache is pure arithmetic, no I/O of its
   own (``_weather_triggers_for_vessel``).
3. Run :meth:`~foreshore.geofence.engine.GeofenceEngine.check` for every vessel and turn
   every resulting proximity, plus every crossed weather threshold, into an
   :class:`~foreshore.models.Alert` — each carrying the real
   :class:`~foreshore.models.Observation`/:class:`~foreshore.models.Provenance` it fired
   on, and a named :class:`~foreshore.models.Handoff` at ``CRITICAL``/``BREACH``.
4. Hand each alert to :class:`~foreshore.push.alerts.AlertStore`, whose dedupe/escalation
   logic decides whether it is actually new to the caller — a boat sitting still 1.8 nm
   from a boundary must not spam a fresh WARN every tick, but an escalation to CRITICAL
   must never be swallowed by that same dedupe.

Only newly-emitted-or-escalated alerts are returned from :meth:`tick` — that is the set a
caller should actually push over the wire (websocket, SMS, whatever channel the demo
wires up later). Every currently-active alert, new or not, is always available from the
:class:`~foreshore.push.alerts.AlertStore` directly.

This module is deliberately synchronous and knows nothing about HTTP, WebSockets or
asyncio — that wiring is a later, separate task. :meth:`run` is a plain blocking loop;
whoever exposes this over a socket can run it in a thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable
from uuid import uuid4

from ..config import RegionConfig, env, load_region, load_vessels
from ..geofence.classes import ALERT_RANK, format_copy, title_for
from ..geofence.engine import GeofenceEngine, GeofenceProximity, shared_engine
from ..models import Alert, AlertLevel, Observation, VesselState, utcnow
from ..tools.geofence_tools import get_exclusion_zones
from ..tools.sea_state import get_sea_state
from ..tools.weather import get_lightning_nowcast, get_weather
from ..verdict.ceiling import regional_handoff
from . import weather_copy
from .alerts import AlertStore
from .vessels import advance, default_fleet

#: env override for the region condition-set cadence, following
#: FORESHORE_PUSH_TICK_SECONDS's naming style. The tick itself runs every few seconds in
#: demo mode (see api/routes_ws.py); fetching sea state / wind / lightning per vessel per
#: tick is not viable at that cadence, so the whole region's weather picture is cached and
#: refreshed on its own, much slower, clock instead. Per-vessel evaluation against that
#: cache is pure arithmetic -- see _weather_triggers_for_vessel.
_DEFAULT_CONDITIONS_REFRESH_S = 600.0


def _conditions_refresh_seconds() -> float:
    override = env("FORESHORE_PUSH_CONDITIONS_SECONDS")
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    return _DEFAULT_CONDITIONS_REFRESH_S


def _threshold_check(
    value: float | None, go_limit: float | None, caution_limit: float | None
) -> tuple[AlertLevel | None, float | None]:
    """Same three-way comparison as ``verdict/engine.py``'s ``_check`` (see lines
    163-186 there), reused rather than reinvented: ``value > caution_limit`` is the
    worse band, ``value > go_limit`` is the lesser one, anything else is within limits.
    Returns ``(None, None)`` for "no trigger" instead of a GO-equivalent level, since a
    push alert only ever exists to report a crossed threshold.
    """
    if value is None or go_limit is None or caution_limit is None:
        return None, None
    if value > caution_limit:
        return "CRITICAL", caution_limit
    if value > go_limit:
        return "WARN", go_limit
    return None, None


@dataclass
class _ConditionSet:
    """The region's cached weather picture, behind ``_refresh_conditions``'s slower
    cadence. Holds the real ``Observation`` each value came from -- never a bare number
    -- so a weather ``Alert.evidence`` always traces to a real ``Provenance``."""

    hs: Observation | None = None
    wind_speed: Observation | None = None
    wind_gust: Observation | None = None
    lightning_active: bool = False
    lightning_observations: list[Observation] = field(default_factory=list)
    lightning_district: str | None = None


@dataclass
class _WeatherTrigger:
    """One vessel-class-threshold crossing, ready to become an ``Alert``."""

    variable: str
    level: AlertLevel
    evidence: list[Observation]
    value: float | None = None
    limit: float | None = None
    district: str | None = None


class PushLoop:
    """Ties the simulated fleet, the geofence engine and the alert store together into
    the request path's one hard-required counterpart: proactive hazard/geofence alerts.
    """

    def __init__(
        self,
        *,
        region: RegionConfig | None = None,
        tick_seconds: float = 60.0,
        fleet: list[VesselState] | None = None,
        alert_store: AlertStore | None = None,
        engine: GeofenceEngine | None = None,
        conditions_refresh_seconds: float | None = None,
    ) -> None:
        self.fleet = fleet if fleet is not None else default_fleet(region)
        self._vessels: dict[str, VesselState] = {v.vessel_id: v for v in self.fleet}
        self.alert_store = alert_store or AlertStore()
        # The process-wide engine, not a private one: the dynamic hazard fences this loop
        # refreshes each tick are exactly what the request path's `check_geofences` needs
        # to see. Two instances meant a vessel inside a live cyclone cone got "no fences
        # nearby" from the boat app while the push loop flagged BREACH for the same point.
        self.engine = engine or shared_engine(region)
        self.region = region or load_region()
        self.tick_seconds = tick_seconds
        # The region weather picture (sea state / wind / lightning), refreshed on its own
        # slower cadence -- see _refresh_conditions. `0` is a legitimate value (tests use
        # it to force a refetch every tick); only an omitted argument falls back to the
        # env-overridable default.
        self.conditions_refresh_seconds = (
            conditions_refresh_seconds
            if conditions_refresh_seconds is not None
            else _conditions_refresh_seconds()
        )
        self._conditions: _ConditionSet | None = None
        self._conditions_fetched_at = None

    # -- the tick ------------------------------------------------------------------

    def tick(self) -> list[Alert]:
        """Advance one tick: move every vessel, refresh dynamic hazard fences once,
        check every vessel against every geofence class, emit new/escalated alerts.

        Returns only the alerts that are actually new-or-escalated this tick (what a
        caller should push over the wire) — not every currently-active alert.
        """
        # 1. Advance every vessel.
        for vessel_id in list(self._vessels.keys()):
            self._vessels[vessel_id] = advance(self._vessels[vessel_id], self.tick_seconds)

        # 2. Refresh dynamic hazard fences once per tick, not once per vessel.
        self._refresh_hazard_fences()

        # 2b. Refresh the cached region weather picture -- on its own, much slower,
        # cadence (see _refresh_conditions). Also once per tick call, never per vessel.
        self._refresh_conditions()

        emitted: list[Alert] = []

        # 3-6. Per vessel: check proximities and weather triggers, upsert alerts, clear
        # stale entries.
        for vessel in self._vessels.values():
            previously_active_keys = {
                a.dedupe_key for a in self.alert_store.active_for_vessel(vessel.vessel_id)
            }

            proximities = self.engine.check(
                vessel.lat, vessel.lon, vessel.heading_deg, vessel.speed_kn
            )

            fresh_keys: set[str] = set()
            for prox in proximities:
                dedupe_key = f"{vessel.vessel_id}:{prox.geofence_class}:{prox.geofence_id}"
                fresh_keys.add(dedupe_key)

                kind = "hazard" if prox.geofence_class == "HAZARD_EXCLUSION" else "geofence"
                handoff = (
                    regional_handoff(
                        f"{prox.name} proximity alert for {vessel.name}.", self.region
                    )
                    if prox.level in ("CRITICAL", "BREACH")
                    else None
                )
                alert = Alert(
                    alert_id=str(uuid4()),
                    vessel_id=vessel.vessel_id,
                    kind=kind,
                    level=prox.level,
                    title_en=title_for(prox.geofence_class, "en"),
                    title_ta=title_for(prox.geofence_class, "ta"),
                    body_en=format_copy(
                        prox.geofence_class,
                        prox.level,
                        "en",
                        name=prox.name,
                        distance_nm=prox.distance_nm,
                        eta_seconds=prox.eta_seconds,
                    ),
                    body_ta=format_copy(
                        prox.geofence_class,
                        prox.level,
                        "ta",
                        name=prox.name,
                        distance_nm=prox.distance_nm,
                        eta_seconds=prox.eta_seconds,
                    ),
                    lat=vessel.lat,
                    lon=vessel.lon,
                    created_at=utcnow(),
                    dedupe_key=dedupe_key,
                    evidence=[self._geofence_observation(prox, vessel)],
                    geofence_class=prox.geofence_class,
                    distance_nm=prox.distance_nm,
                    eta_seconds=prox.eta_seconds,
                    handoff=handoff,
                    surface_languages=self.region.surface_languages,
                )

                result = self.alert_store.upsert(alert)
                if result is not None:
                    emitted.append(result)

            for trigger in self._weather_triggers_for_vessel(vessel):
                dedupe_key = f"{vessel.vessel_id}:weather:{trigger.variable}:{trigger.level}"
                fresh_keys.add(dedupe_key)

                handoff = (
                    regional_handoff(
                        f"{weather_copy.title_for(trigger.variable, 'en')} affecting "
                        f"{vessel.name}.",
                        self.region,
                    )
                    if trigger.level in ("CRITICAL", "BREACH")
                    else None
                )
                alert = Alert(
                    alert_id=str(uuid4()),
                    vessel_id=vessel.vessel_id,
                    kind="weather",
                    level=trigger.level,
                    title_en=weather_copy.title_for(trigger.variable, "en"),
                    title_ta=weather_copy.title_for(trigger.variable, "ta"),
                    body_en=weather_copy.format_copy(
                        trigger.variable, trigger.level, "en",
                        value=trigger.value, limit=trigger.limit, district=trigger.district,
                    ),
                    body_ta=weather_copy.format_copy(
                        trigger.variable, trigger.level, "ta",
                        value=trigger.value, limit=trigger.limit, district=trigger.district,
                    ),
                    lat=vessel.lat,
                    lon=vessel.lon,
                    created_at=utcnow(),
                    dedupe_key=dedupe_key,
                    evidence=list(trigger.evidence),
                    handoff=handoff,
                    surface_languages=self.region.surface_languages,
                )

                result = self.alert_store.upsert(alert)
                if result is not None:
                    emitted.append(result)

            # 6. Clear stale entries: fences/weather triggers that were active before
            # this tick but are no longer in range or no longer crossed at all (the
            # vessel moved out of range, or conditions recovered / the level changed),
            # so a later re-approach or re-crossing fires fresh rather than staying
            # suppressed forever.
            for stale_key in previously_active_keys - fresh_keys:
                self.alert_store.clear(stale_key)

        return emitted

    def fleet_snapshot(self) -> list[VesselState]:
        """Current, post-tick position of every tracked vessel, in the same order as
        ``self.fleet``.

        ``self.fleet`` is fixed at construction time and does not itself reflect ticks —
        the live, per-tick-advanced state lives in the internal vessel map instead. This
        is a read accessor for callers (e.g. the API layer's ``GET /api/fleet`` and its
        WebSocket fleet broadcast) that need current fleet state without triggering a
        tick of their own.
        """
        return [self._vessels[v.vessel_id] for v in self.fleet]

    def _refresh_hazard_fences(self) -> None:
        """Refresh the engine's dynamic ``HAZARD_EXCLUSION`` fences from
        ``get_exclusion_zones``, once per tick.

        A raising tool call or a payload with zero hazard features are both valid
        outcomes (no active hazard) and must not crash the tick — they degrade to
        ``set_dynamic([])``, clearing any previous hazard geometry rather than
        accumulating it tick over tick.
        """
        self.engine.clear_dynamic()
        hazard_fences: list = []
        try:
            result = get_exclusion_zones()
            features = (result.payload or {}).get("features", []) or []
            hazard_features = [
                f
                for f in features
                if (f.get("properties") or {}).get("geofence_class") == "HAZARD_EXCLUSION"
            ]
            hazard_fences = self.engine.dynamic_from_features(hazard_features)
        except Exception:  # noqa: BLE001 — no active hazard is a valid outcome, not a crash
            hazard_fences = []
        self.engine.set_dynamic(hazard_fences)

    def _refresh_conditions(self) -> None:
        """Refresh the cached region weather picture (sea state, wind, IMD lightning
        nowcast) on its own cadence -- ``self.conditions_refresh_seconds``, default
        :data:`_DEFAULT_CONDITIONS_REFRESH_S` (600 s), env-overridable via
        ``FORESHORE_PUSH_CONDITIONS_SECONDS``.

        The tick itself runs every few seconds in demo mode; fetching sea state, wind
        and the IMD nowcast per vessel per tick is not viable at that cadence, so this
        fetches once for the whole region and every vessel's threshold check
        (``_weather_triggers_for_vessel``) reads the cached result with no I/O of its
        own.

        Mirrors ``_refresh_hazard_fences``'s failure posture exactly: the whole fetch
        is one try block, and *any* exception anywhere in it -- one source's outage or
        three -- degrades to "no conditions known" (an empty ``_ConditionSet``) rather
        than a partially-stale cache or a tick-killing exception. The three underlying
        tools already degrade internally (``get_weather``/``get_sea_state``/
        ``get_lightning_nowcast`` document that they never raise), so this only ever
        fires on the same kind of catastrophic failure ``_refresh_hazard_fences``
        guards against.
        """
        now = utcnow()
        if (
            self._conditions is not None
            and self._conditions_fetched_at is not None
            and (now - self._conditions_fetched_at).total_seconds()
            < self.conditions_refresh_seconds
        ):
            return

        conditions = _ConditionSet()
        try:
            lat, lon = self.region.centre

            sea = get_sea_state(lat, lon)
            conditions.hs = next(
                (o for o in sea.observations
                 if o.variable == "significant_wave_height" and o.is_numeric),
                None,
            )

            weather = get_weather(lat, lon)
            conditions.wind_speed = next(
                (o for o in weather.observations if o.variable == "wind_speed" and o.is_numeric),
                None,
            )
            conditions.wind_gust = next(
                (o for o in weather.observations if o.variable == "wind_gust" and o.is_numeric),
                None,
            )

            district = self.region.district_for(lat, lon)
            nowcast = get_lightning_nowcast(district=district, lat=lat, lon=lon)
            # get_lightning_nowcast is the system's ONLY lightning authority (CLAUDE.md:
            # CAPE is never a lightning probability and must never be substituted here).
            # `lightning_assessable` is False on any abstention (adapter unreachable, no
            # feature for the district, ...); only when it is True is `observations`
            # trusted to mean anything, and only a non-"NIL" category counts as active.
            if (nowcast.payload or {}).get("lightning_assessable"):
                active = [
                    o for o in nowcast.observations
                    if str(o.value).strip().upper() != "NIL"
                ]
                conditions.lightning_active = bool(active)
                conditions.lightning_observations = active
                if active:
                    conditions.lightning_district = str(
                        active[0].qualifiers.get("district")
                        or (nowcast.payload or {}).get("district")
                        or district
                        or ""
                    ) or None
        except Exception:  # noqa: BLE001 — degrade to "no conditions known", never kill the tick
            conditions = _ConditionSet()

        self._conditions = conditions
        self._conditions_fetched_at = now

    def _weather_triggers_for_vessel(self, vessel: VesselState) -> list[_WeatherTrigger]:
        """Pure arithmetic: compare the cached region condition set against this
        vessel's own class limits (``config/vessels.yaml`` via ``load_vessels()`` /
        ``VesselClass.limit``). No I/O -- everything here already lives in
        ``self._conditions``, refreshed separately by ``_refresh_conditions``.

        Reuses exactly the thresholds ``verdict/engine.py:245-266`` already applies to
        the same variables (``hs_go_m``/``hs_caution_m``, ``wind_go_kn``/
        ``wind_caution_kn``, and gust checked against ``wind_caution_kn``/
        ``gust_caution_kn`` -- gust's own "go" band is the wind-speed caution limit,
        exactly as the verdict engine has it) -- never a number invented here.
        """
        conditions = self._conditions
        if conditions is None:
            return []
        vessel_class = load_vessels().get(vessel.vessel_class)
        triggers: list[_WeatherTrigger] = []

        if conditions.hs is not None:
            level, limit = _threshold_check(
                conditions.hs.numeric,
                vessel_class.limit("hs_go_m"),
                vessel_class.limit("hs_caution_m"),
            )
            if level is not None:
                triggers.append(_WeatherTrigger(
                    variable="significant_wave_height", level=level,
                    evidence=[conditions.hs], value=conditions.hs.numeric, limit=limit,
                ))

        # Wind speed and gusts are one PS-vocabulary trigger ("adverse weather"), the
        # worse of the two governs the level, and both observations that were actually
        # checked ride along as evidence.
        wind_level: AlertLevel | None = None
        wind_limit: float | None = None
        wind_value: float | None = None
        wind_evidence: list[Observation] = []
        if conditions.wind_speed is not None:
            level, limit = _threshold_check(
                conditions.wind_speed.numeric,
                vessel_class.limit("wind_go_kn"),
                vessel_class.limit("wind_caution_kn"),
            )
            wind_evidence.append(conditions.wind_speed)
            if level is not None and (wind_level is None or ALERT_RANK[level] > ALERT_RANK[wind_level]):
                wind_level, wind_limit, wind_value = level, limit, conditions.wind_speed.numeric
        if conditions.wind_gust is not None:
            level, limit = _threshold_check(
                conditions.wind_gust.numeric,
                vessel_class.limit("wind_caution_kn"),
                vessel_class.limit("gust_caution_kn"),
            )
            wind_evidence.append(conditions.wind_gust)
            if level is not None and (wind_level is None or ALERT_RANK[level] > ALERT_RANK[wind_level]):
                wind_level, wind_limit, wind_value = level, limit, conditions.wind_gust.numeric
        if wind_level is not None:
            triggers.append(_WeatherTrigger(
                variable="wind", level=wind_level, evidence=wind_evidence,
                value=wind_value, limit=wind_limit,
            ))

        if conditions.lightning_active:
            # Binary by construction (see get_lightning_nowcast): there is no partial
            # reading between "no active nowcast warning" and one, so this fires
            # straight to CRITICAL rather than inventing an intermediate WARN band.
            triggers.append(_WeatherTrigger(
                variable="lightning", level="CRITICAL",
                evidence=list(conditions.lightning_observations),
                district=conditions.lightning_district,
            ))

        return triggers

    def _geofence_observation(self, prox: GeofenceProximity, vessel: VesselState) -> Observation:
        """Wrap a geofence proximity as a sourced ``Observation`` for ``Alert.evidence``.

        The distance is a live measurement against real, already-fetched boundary
        geometry, so it carries ``prox.provenance`` -- the real record the geofence
        engine already attached (see ``geofence/engine.py::_layer_provenance``) -- never
        a synthesised one.
        """
        return Observation(
            variable="geofence_distance_nm",
            value=round(prox.distance_nm, 3),
            unit="nm",
            lat=prox.closest_lat if prox.closest_lat is not None else vessel.lat,
            lon=prox.closest_lon if prox.closest_lon is not None else vessel.lon,
            valid_time=vessel.updated_at,
            provenance=prox.provenance,
            qualifiers={
                "geofence_id": prox.geofence_id,
                "geofence_class": prox.geofence_class,
                "inside": prox.inside,
            },
        )

    # -- the loop --------------------------------------------------------------------

    def run(self, on_alert: Callable[[Alert], None], *, iterations: int | None = None) -> None:
        """Synchronous loop: call :meth:`tick` every ``tick_seconds`` (``time.sleep``
        between ticks), invoke ``on_alert(alert)`` for each newly emitted alert.

        Stops after ``iterations`` ticks if given, otherwise loops forever — a caller
        wanting the demo's 5 s cadence just passes ``tick_seconds=5.0`` at construction;
        this module has no separate notion of "demo mode". Kept synchronous/blocking on
        purpose — whoever wires this to a websocket later can run it in a thread.
        """
        count = 0
        while iterations is None or count < iterations:
            for alert in self.tick():
                on_alert(alert)
            count += 1
            if iterations is None or count < iterations:
                time.sleep(self.tick_seconds)


__all__ = ["PushLoop"]
