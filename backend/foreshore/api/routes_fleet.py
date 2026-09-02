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

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

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


__all__ = ["router"]
