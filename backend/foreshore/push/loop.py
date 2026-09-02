"""The push/alert loop — FORESHORE's proactive path.

CLAUDE.md is explicit that a request-response-only system fails the problem statement:
the push path (a background scan over tracked vessel positions, firing hazard and
geofence-approach alerts *before* a fisherman asks) is a scored requirement, not a nice
extra. This module is where that scan actually happens.

Each :meth:`PushLoop.tick` does four things, in order, every time it runs:

1. Dead-reckon every tracked vessel forward one tick (:func:`foreshore.push.vessels.advance`
   — pure, deterministic, no network).
2. Refresh the engine's dynamic hazard fences **once per tick, not once per vessel** —
   GDACS cyclone geometry does not change vessel-to-vessel, so re-fetching it per boat
   would be eight times the work for the same answer. Only ``HAZARD_EXCLUSION``-classed
   features from ``get_exclusion_zones`` become dynamic fences; the static IMBL/MPA/eco
   layers are already checked natively by :class:`~foreshore.geofence.engine.GeofenceEngine`
   against the vector store, so re-adding them here would double-count the same boundary
   as two separate hits under two different mechanisms.
3. Run :meth:`~foreshore.geofence.engine.GeofenceEngine.check` for every vessel and turn
   every resulting proximity into an :class:`~foreshore.models.Alert`.
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
from typing import Callable
from uuid import uuid4

from ..config import RegionConfig, load_region
from ..geofence.classes import format_copy, title_for
from ..geofence.engine import GeofenceEngine
from ..models import Alert, VesselState, utcnow
from ..tools.geofence_tools import get_exclusion_zones
from .alerts import AlertStore
from .vessels import advance, default_fleet


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
    ) -> None:
        self.fleet = fleet if fleet is not None else default_fleet(region)
        self._vessels: dict[str, VesselState] = {v.vessel_id: v for v in self.fleet}
        self.alert_store = alert_store or AlertStore()
        self.engine = engine or GeofenceEngine(region=region)
        self.region = region or load_region()
        self.tick_seconds = tick_seconds

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

        emitted: list[Alert] = []

        # 3-6. Per vessel: check proximities, upsert alerts, clear stale entries.
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
                    evidence=[],
                    geofence_class=prox.geofence_class,
                    distance_nm=prox.distance_nm,
                    eta_seconds=prox.eta_seconds,
                    handoff=None,
                )

                result = self.alert_store.upsert(alert)
                if result is not None:
                    emitted.append(result)

            # 6. Clear stale entries: fences that were active before this tick but are
            # no longer in range at all (the vessel moved out of range entirely), so a
            # later re-approach fires fresh rather than staying suppressed forever.
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
