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

import logging
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


log = logging.getLogger("foreshore.api")


def _announce() -> None:
    """Say, at startup, what this process will actually do.

    Both of these degrade silently and correctly by design — fixture mode replays a
    frozen snapshot, and a missing provider key falls back to the deterministic scripted
    client — which meant the only symptom of a server running neither live nor through a
    model was a suspiciously fast answer in flat prose. That is not a diagnosis anyone
    should have to make from the outside.
    """
    try:
        from ..agents.runtime import make_client

        client = make_client()
    except Exception as exc:  # noqa: BLE001 — never let logging stop the boot
        log.warning("LLM client unavailable: %s: %s", type(exc).__name__, exc)
        return

    log.info("FORESHORE mode=%s llm=%s", mode(), client.name)
    if mode() != "live":
        log.warning(
            "FORESHORE_MODE=%s — answers replay frozen snapshots from data/fixtures/ and "
            "will report their snapshot date, not today's. Unset it for live sources.",
            mode(),
        )
    if client.name == "scripted":
        log.warning(
            "No provider key for FORESHORE_LLM_PROVIDER — answers will be composed from "
            "templates. The verdict, evidence and trace are unaffected; only the prose is."
        )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.traces = TraceStore()
    _announce()
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

    # Which model is actually in the loop, named. A server started without its provider
    # key falls back to the scripted client and still answers correctly — by design — so
    # the only visible symptom is flat template prose and a suspiciously fast response.
    # That is not something anyone should have to infer.
    try:
        from ..agents.runtime import make_client

        client = make_client()
        llm = {"client": client.name, "model_in_loop": client.name != "scripted"}
    except Exception as exc:  # noqa: BLE001 — health must never become an outage itself
        llm = {"client": f"unavailable: {type(exc).__name__}", "model_in_loop": False}

    return {
        "mode": mode(),
        "region_id": region_id,
        "llm": llm,
        "sources": sources,
        "tools_unavailable": failed_modules(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        **({"note": region_note} if region_note else {}),
    }


__all__ = ["app"]
