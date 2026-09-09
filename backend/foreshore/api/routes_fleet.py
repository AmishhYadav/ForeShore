"""Fleet and push-path REST surface — ``GET /api/fleet``, ``GET /api/alerts`` and
``POST /api/alerts/{alert_id}/ack``.

The live push loop itself runs on a background thread (wired in ``api/main.py``'s
lifespan) and pushes over ``WS /ws/alerts``; these three endpoints are the synchronous,
poll-if-you-must counterpart the doc promises — the same ``PushLoop``/``AlertStore``
instances the background thread drives, read here rather than duplicated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..models import Alert, AlertLevel

router = APIRouter(prefix="/api", tags=["fleet"])


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------------------------
# GET /api/fleet
# ------------------------------------------------------------------------------------


@router.get("/fleet")
def get_fleet(request: Request) -> dict[str, Any]:
    push_loop = request.app.state.push_loop
    vessels = push_loop.fleet_snapshot()
    return {
        "vessels": [v.to_dict() for v in vessels],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ------------------------------------------------------------------------------------
# GET /api/alerts?vessel_id=&active=true&since=
# ------------------------------------------------------------------------------------


@router.get("/alerts")
def get_alerts(
    request: Request,
    vessel_id: str | None = None,
    active: bool = True,
    since: str | None = None,
) -> dict[str, Any]:
    store = request.app.state.alert_store
    since_dt = _parse_since(since)

    if active:
        alerts = store.active_for_vessel(vessel_id) if vessel_id else store.all_active()
        if since_dt is not None:
            alerts = [a for a in alerts if a.created_at >= since_dt]
    else:
        # active=false -> the history view: every alert ever upserted (acknowledged and
        # cleared ones included), not just the currently-open table.
        alerts = store.history(vessel_id=vessel_id, since=since_dt)

    return {"alerts": [a.to_dict() for a in alerts]}


# ------------------------------------------------------------------------------------
# POST /api/alerts/{alert_id}/ack
# ------------------------------------------------------------------------------------


class AckRequest(BaseModel):
    by: str = "unknown"


@router.post("/alerts/{alert_id}/ack")
def post_alert_ack(
    alert_id: str, request: Request, body: AckRequest = AckRequest()
) -> dict[str, Any]:
    store = request.app.state.alert_store
    alert = store.acknowledge(alert_id, by=body.by)
    if alert is None:
        raise HTTPException(
            status_code=404, detail=f"no active alert with id {alert_id!r}"
        )
    return alert.to_dict()


# ------------------------------------------------------------------------------------
# POST /api/alerts/broadcast — console-issued live alert over the same push transport
#
# This is the "console to board UI" alert PS bullet 7/8 needs a human-in-the-loop
# counterpart for: a watchstander who has seen something the automated scan has not
# (radioed report, visual sighting, a bulletin update) can put it in front of the fleet
# immediately, on the exact same WS/ws/alerts channel and AlertStore the automated
# geofence/weather/hazard alerts already use — so every client (boat UI, any phone
# running it, another console tab) that already renders an Alert renders this one with
# no new code. No new transport, no polling: `_Broadcaster.publish` bypasses the tick
# entirely so this reaches connected clients the instant the operator submits it.
# ------------------------------------------------------------------------------------


class BroadcastAlertRequest(BaseModel):
    #: Omit to broadcast to every vessel currently in the fleet snapshot.
    vessel_id: str | None = None
    level: AlertLevel = "WARN"
    title: str
    body: str
    by: str = "console"


@router.post("/alerts/broadcast")
def post_alert_broadcast(body: BroadcastAlertRequest, request: Request) -> dict[str, Any]:
    push_loop = request.app.state.push_loop
    store = request.app.state.alert_store
    broadcaster = getattr(request.app.state, "ws_broadcaster", None)

    vessels = push_loop.fleet_snapshot()
    if body.vessel_id is not None:
        vessels = [v for v in vessels if v.vessel_id == body.vessel_id]
        if not vessels:
            raise HTTPException(
                status_code=404, detail=f"no tracked vessel with id {body.vessel_id!r}"
            )

    now = datetime.now(timezone.utc)
    broadcast_id = uuid4().hex[:8]
    sent: list[dict[str, Any]] = []
    for vessel in vessels:
        alert = Alert(
            alert_id=str(uuid4()),
            vessel_id=vessel.vessel_id,
            kind="operator",
            level=body.level,
            title_en=body.title,
            title_ta=body.title,
            body_en=body.body,
            body_ta=body.body,
            lat=vessel.lat,
            lon=vessel.lon,
            created_at=now,
            # Unique per send, never suppressed: an operator broadcast is a deliberate
            # act each time, not a re-scan of a still-true condition — the dedupe that
            # protects the automated path (CLAUDE.md push-loop discipline) does not
            # apply here.
            dedupe_key=f"operator:{broadcast_id}:{vessel.vessel_id}",
        )
        store.upsert(alert)
        if broadcaster is not None:
            broadcaster.publish({"type": "alert", "alert": alert.to_dict()}, vessel_id=vessel.vessel_id)
        sent.append(alert.to_dict())

    return {"broadcast_id": broadcast_id, "sent": len(sent), "alerts": sent}


__all__ = ["router"]
