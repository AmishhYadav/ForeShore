"""``WS /ws/alerts`` — the push path's live transport, and the background thread that
drives it.

CLAUDE.md: a request-response-only system fails the problem statement. ``push/loop.py``
is deliberately synchronous ("whoever exposes this over a socket can run it in a
thread") — this module is that later, separate task. It owns exactly one concurrency
boundary: :class:`PushLoop` ticks on a plain background thread; every connected
WebSocket client is served from the asyncio event loop. The two meet through
``asyncio.Queue`` objects fed via ``loop.call_soon_threadsafe`` — the only
thread-safe way to hand data from a foreign thread into an asyncio queue.

Message shapes are exactly ``docs/API.md``'s ``WS /ws/alerts`` section:
``{"type": "hello", ...}`` once per connection, then ``{"type": "alert", "alert": ...}``
and ``{"type": "vessels", "vessels": [...], "ts": ...}`` as the loop ticks. Client ->
server: ``{"type": "ack", "alert_id", "by"}`` and ``{"type": "subscribe", "vessel_ids"}``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect

from ..config import RegionConfig, env, is_fixture, load_region, mode
from ..push.alerts import AlertStore
from ..push.loop import PushLoop

log = logging.getLogger("foreshore.api.ws")

router = APIRouter()

#: env override for the tick cadence; PLAN.md: "Every 60 s (5 s in demo mode)". Demo mode
#: is inferred from FORESHORE_MODE=fixture — the same switch that makes a live venue-wifi
#: outage harmless already implies "this is the rehearsed demo, not a long-running deploy".
_DEFAULT_LIVE_TICK_S = 60.0
_DEFAULT_DEMO_TICK_S = 5.0


def _tick_seconds() -> float:
    override = env("FORESHORE_PUSH_TICK_SECONDS")
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    return _DEFAULT_DEMO_TICK_S if is_fixture() else _DEFAULT_LIVE_TICK_S


class _Broadcaster:
    """Every connected client's inbound queue, fed from the (foreign) push-loop thread.

    ``publish`` is the only method the background thread calls, and it never awaits —
    ``call_soon_threadsafe`` schedules the enqueue on the owning event loop and returns
    immediately, so a slow or stalled client can never back-pressure the push-loop tick.
    """

    def __init__(self, event_loop: asyncio.AbstractEventLoop) -> None:
        self._event_loop = event_loop
        self._queues: dict[asyncio.Queue, set[str] | None] = {}
        self._lock = threading.Lock()

    def register(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._queues[q] = None  # None = subscribed to every vessel
        return q

    def unregister(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._queues.pop(q, None)

    def set_subscription(self, q: asyncio.Queue, vessel_ids: list[str] | None) -> None:
        with self._lock:
            if q in self._queues:
                self._queues[q] = set(vessel_ids) if vessel_ids else None

    def _wants(self, subscription: set[str] | None, vessel_id: str | None) -> bool:
        if subscription is None or vessel_id is None:
            return True
        return vessel_id in subscription

    def publish(self, message: dict[str, Any], *, vessel_id: str | None = None) -> None:
        """Thread-safe: called from the push-loop background thread."""
        with self._lock:
            targets = list(self._queues.items())
        for q, subscription in targets:
            if not self._wants(subscription, vessel_id):
                continue
            self._event_loop.call_soon_threadsafe(q.put_nowait, message)


def start_push_loop_background(
    app: FastAPI,
    *,
    region: RegionConfig | None = None,
    tick_seconds: float | None = None,
) -> None:
    """Wire the fleet simulator + geofence engine + alert store to the live process.

    Called once, from ``api/main.py``'s startup. Populates ``app.state.push_loop`` and
    ``app.state.alert_store`` (the same instances ``routes_fleet.py``'s REST endpoints
    read), and starts one daemon thread that ticks the loop forever, broadcasting each
    tick's new alerts and the fresh fleet snapshot to every connected ``/ws/alerts``
    client. A daemon thread needs no explicit shutdown — it dies with the process, which
    is the right lifetime for a hackathon demo process with no graceful-drain requirement.
    """
    region = region or load_region()
    resolved_tick = tick_seconds if tick_seconds is not None else _tick_seconds()
    loop = PushLoop(region=region, tick_seconds=resolved_tick, alert_store=AlertStore())

    app.state.push_loop = loop
    app.state.alert_store = loop.alert_store
    app.state.ws_broadcaster = _Broadcaster(asyncio.get_event_loop())
    app.state.push_loop_region_id = region.region_id
    app.state.push_loop_tick_seconds = resolved_tick

    def _run() -> None:
        broadcaster: _Broadcaster = app.state.ws_broadcaster
        while True:
            try:
                alerts = loop.tick()
                for alert in alerts:
                    broadcaster.publish(
                        {"type": "alert", "alert": alert.to_dict()}, vessel_id=alert.vessel_id
                    )
                vessels = loop.fleet_snapshot()
                broadcaster.publish(
                    {
                        "type": "vessels",
                        "vessels": [v.to_dict() for v in vessels],
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                )
            except Exception:  # noqa: BLE001 — one bad tick must not kill the demo's loop
                log.exception("push loop tick failed; continuing")
            time.sleep(loop.tick_seconds)

    thread = threading.Thread(target=_run, name="foreshore-push-loop", daemon=True)
    thread.start()
    app.state.push_loop_thread = thread


@router.websocket("/ws/alerts")
async def ws_alerts(websocket: WebSocket) -> None:
    app: FastAPI = websocket.app
    broadcaster: _Broadcaster | None = getattr(app.state, "ws_broadcaster", None)
    if broadcaster is None:
        await websocket.close(code=1011, reason="push loop not started")
        return

    await websocket.accept()
    queue = broadcaster.register()
    region_id = getattr(app.state, "push_loop_region_id", None)
    tick_seconds = getattr(app.state, "push_loop_tick_seconds", _DEFAULT_LIVE_TICK_S)
    await websocket.send_json(
        {
            "type": "hello",
            "interval_s": tick_seconds,
            "mode": mode(),
            "region_id": region_id,
        }
    )

    async def _sender() -> None:
        while True:
            message = await queue.get()
            await websocket.send_json(message)

    sender_task = asyncio.ensure_future(_sender())
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            if msg_type == "ack":
                alert_id = data.get("alert_id")
                by = data.get("by", "unknown")
                store: AlertStore | None = getattr(app.state, "alert_store", None)
                if store is not None and alert_id:
                    store.acknowledge(alert_id, by=by)
            elif msg_type == "subscribe":
                broadcaster.set_subscription(queue, data.get("vessel_ids"))
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 — a malformed client message must not crash the server
        log.exception("ws_alerts connection error")
    finally:
        sender_task.cancel()
        broadcaster.unregister(queue)


__all__ = ["router", "start_push_loop_background"]
