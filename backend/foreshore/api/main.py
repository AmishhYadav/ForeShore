"""FastAPI entrypoint — wires every router in ``docs/API.md`` onto one app and starts
the push loop's background thread.

Both surfaces (boat UI, shore console) call the same app on the same port; the contract
this glues together is fixed in ``docs/API.md``, not decided here. This module's own job
is small on purpose: CORS for the two Vite dev servers, shared ``app.state`` (the one
:class:`~foreshore.push.loop.PushLoop`, :class:`~foreshore.push.alerts.AlertStore` and
:class:`~foreshore.store.traces.TraceStore` every route module reads), the four routers,
and ``GET /health``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import load_region, mode
from ..store.traces import TraceStore
from ..tools import failed_modules
from ..tools.discovery import list_available_data
from . import routes_fleet, routes_query, routes_reference, routes_ws


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.traces = TraceStore()
    routes_ws.start_push_loop_background(app)
    yield


app = FastAPI(title="FORESHORE", version="0.1.0", lifespan=_lifespan)

# Vite defaults: 5173 (boat) / 5174 (console) when run as two dev servers, plus the
# common alternate 3000. Wide open on purpose — this is a hackathon demo API with no
# auth surface, never a deployed multi-tenant service.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes_query.router)
app.include_router(routes_fleet.router)
app.include_router(routes_reference.router)
app.include_router(routes_ws.router)


@app.get("/health")
def health() -> dict[str, Any]:
    """Reports source reachability; never becomes an outage itself — every branch below
    degrades to a value rather than raising, mirroring ``scripts/healthcheck.py``'s own
    per-source isolation."""
    try:
        region = load_region()
        region_id = region.region_id
    except Exception as exc:  # noqa: BLE001
        region_id = None
        region_note = f"{type(exc).__name__}: {exc}"
    else:
        region_note = None

    try:
        result = list_available_data()
        sources = [
            {
                "source_id": obs.provenance.source_id,
                "ok": bool(obs.qualifiers.get("ok")),
                "latency_ms": obs.qualifiers.get("latency_ms"),
                "issued_at": obs.provenance.issued_at.isoformat()
                if obs.provenance.issued_at
                else None,
                "freshness": obs.provenance.freshness,
            }
            for obs in result.observations
        ]
    except Exception as exc:  # noqa: BLE001
        sources = []
        region_note = region_note or f"source probe failed: {type(exc).__name__}: {exc}"

    return {
        "mode": mode(),
        "region_id": region_id,
        "sources": sources,
        "tools_unavailable": failed_modules(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        **({"note": region_note} if region_note else {}),
    }


__all__ = ["app"]
