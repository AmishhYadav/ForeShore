"""Freeze live source snapshots into ``data/fixtures/`` for the network-off demo path.

CLAUDE.md: *"FORESHORE_MODE=live|fixture. Every source adapter respects it. Fixture mode
replays frozen snapshots from data/fixtures/, so a live demo cannot die on venue wifi."*
This script is what actually makes that true. Before it is run, ``data/fixtures/`` is
empty and every tool call in fixture mode abstains for lack of a frozen snapshot — this
script closes that gap by hitting every source adapter for the exact demo scenarios in
``PLAN.md``'s 7-minute script, then promoting whatever landed in ``data/cache/`` into
``data/fixtures/``.

Two-phase, mirroring ``scripts/fetch_static.py``'s own shape (warm, then write):

1. **Warm.** Every registered tool (``backend/foreshore/tools/``, 16 of them) is called
   directly through the process-wide tool registry with the actual demo arguments —
   Rameswaram (the region's primary anchor port), the region bbox, and a fishing-ground
   destination south-east of it — so every source adapter's ``Source.get`` fires a real
   live fetch and writes a snapshot to ``data/cache/``. On top of that, the three demo
   ``POST /api/query`` scenarios are run end to end through
   ``foreshore.agents.orchestrator.answer`` (the exact function ``routes_query.py`` calls
   for that endpoint — calling it directly is equivalent to curling the endpoint, minus
   the HTTP hop, and avoids needing a running server), plus the reference/fleet/health
   surfaces (``GET /api/region``, ``GET /api/geofences.geojson``, ``GET /api/fleet``,
   ``GET /health``) via the same functions those routes call. Every warm step is isolated
   — see :func:`_call_isolated` — so one source's outage never stops the rest from
   warming, the same discipline ``scripts/healthcheck.py`` and ``scripts/fetch_static.py``
   already use.
2. **Promote.** :func:`foreshore.store.cache.promote_cache_to_fixture` (an existing,
   unmodified Phase-0/1 function — this script only calls it, never reimplements it)
   copies the newest live JSON snapshot for every ``(source_id, key)`` pair actually
   warmed into ``data/fixtures/``. INCOIS OSF grid subsets are a second, separate
   contract (``store.cache.cache_binary``/``binary_path`` — large NetCDF blobs, not JSON
   snapshots, kept out of the timestamped-history mechanism on purpose per that module's
   own docstring), so this script mirrors ``data/cache/<source>/blobs/*.nc`` into
   ``data/fixtures/<source>/blobs/*.nc`` itself — a plain file copy, since a blob's
   content-addressed key already makes it a single canonical file with no "latest"
   variant to pick.

Run in ``FORESHORE_MODE=live`` only — it refuses to run under ``fixture`` mode (see the
guard at the top of ``main``, before any ``foreshore`` import) because its entire purpose
is to create the fixtures fixture mode later replays, not replay them itself.

CLI
---
    python scripts/freeze_fixtures.py [--region palk_bay_gom]

Exit code 0 always: an individual source or query failing (an INCOIS outage, a 403, an
empty PFZ line for today) is itself real, honest data worth freezing — see CLAUDE.md's
own note that "0 features when no active cyclone is valid, not an error." This script
only fails hard if it cannot even load region config or import the tool registry.
"""

from __future__ import annotations

import os

# Refuse fixture mode, then force live — before any `foreshore` import. This script's
# entire reason to exist is to CREATE the fixtures fixture mode replays; running it
# under fixture mode would make every "live" fetch below silently replay old fixtures
# (or raise FixtureMissing) instead of hitting the network, defeating the point.
_requested_mode = os.environ.get("FORESHORE_MODE", "").strip().lower()
if _requested_mode == "fixture":
    import sys as _sys

    print(
        "ERROR: scripts/freeze_fixtures.py refuses to run under FORESHORE_MODE=fixture — "
        "it exists to CREATE live snapshots for fixture mode, not replay them. Unset "
        "FORESHORE_MODE or set it to 'live' and re-run.",
        file=_sys.stderr,
    )
    raise SystemExit(1)
os.environ["FORESHORE_MODE"] = "live"

import argparse
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Make `import foreshore` work whether or not the package is installed editable into the
# active venv — same defensive sys.path insertion scripts/healthcheck.py and
# scripts/fetch_static.py both use.
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from foreshore.config import CACHE_DIR, FIXTURE_DIR, Port, RegionConfig, load_region  # noqa: E402
from foreshore.store import cache as cache_store  # noqa: E402
from foreshore.tools import failed_modules, registry  # noqa: E402

# ------------------------------------------------------------------------------------
# Demo scenario text — the exact three queries PLAN.md's 7-minute script exercises,
# minus the Tamil ASR/TTS front end (that is a separate, later concern; this script
# talks straight to the agent core the same way the frontend's POST /api/query does).
# ------------------------------------------------------------------------------------

#: "Is it safe to go out to sea tomorrow morning?" — same wording pattern as PLAN.md's
#: demo-script line 0:00. Deliberately uses "போகலாமா" — literally one of the
#: safety_check intent cues in agents/planner.py's INTENT_CUES, so this is not just
#: plausible Tamil, it is text the planner is documented to recognise.
SAFETY_QUERY_TA = "நாளை காலை கடலுக்குப் போகலாமா?"

#: PLAN.md demo-script line 1:45.
FISHING_ZONE_QUERY_EN = "Where's the nearest fishing zone?"

#: The SAMUDRA-wedge differentiator query, PLAN.md demo-script line 5:45.
PRODUCTIVITY_QUERY_EN = "Why has fish productivity declined in this region over the past few years?"


@dataclass
class WarmRow:
    """One warmed thing: a tool call, a demo query, or a reference/fleet/health read."""

    label: str
    ok: bool
    note: str
    elapsed_ms: int


def _call_isolated(label: str, fn: Any) -> WarmRow:
    """Run one warm step, translating any exception into a failed row.

    Mirrors scripts/fetch_static.py's run_layer and scripts/healthcheck.py's _check:
    one bad source/query must never abort the rest of the warm pass.
    """
    t0 = time.perf_counter()
    try:
        note = fn()
        elapsed = int((time.perf_counter() - t0) * 1000)
        return WarmRow(label=label, ok=True, note=str(note)[:160], elapsed_ms=elapsed)
    except Exception as exc:  # noqa: BLE001 - isolation is the point
        elapsed = int((time.perf_counter() - t0) * 1000)
        return WarmRow(label=label, ok=False, note=f"{type(exc).__name__}: {exc}"[:160], elapsed_ms=elapsed)


# ------------------------------------------------------------------------------------
# Phase 1a — every registered tool, called directly with real demo arguments.
# ------------------------------------------------------------------------------------


def _tool_args_by_name(region: RegionConfig, origin: Port, dest: tuple[float, float]) -> dict[str, dict[str, Any]]:
    """Best-effort call arguments per registered tool, built from the actual demo
    scenario: Rameswaram (``region.anchor_ports[0]``) as the vessel position, the
    region's own bbox for every bbox-shaped tool, and a fishing-ground destination
    south-east of Rameswaram for the router. This is the same shape
    ``backend/tests/test_provenance.py``'s ``_TOOL_ARGS_BY_NAME`` uses for its registry
    sweep, extended here with the bbox/destination/vessel-position args a plain
    lat/lon call does not cover (route, productivity, PFZ derivation, exclusions,
    hazards)."""
    pos = {"lat": origin.lat, "lon": origin.lon}
    bbox = list(region.bbox)
    return {
        "get_governing_advisory": dict(pos),
        "get_sea_state": {**pos, "when": None},
        "get_weather": {**pos, "when": None},
        "get_lightning_nowcast": {"district": None, **pos},
        "get_tide": {**pos, "hours": 24},
        "get_currents": {**pos, "when": None},
        "find_nearest_pfz": dict(pos),
        "derive_pfz_zones": {"bbox": bbox, "when": None},
        "check_geofences": {**pos, "heading_deg": 60.0, "speed_kn": 6.0, "classes": None},
        "get_exclusion_zones": {"when": None, "bbox": bbox},
        "plan_route": {
            "origin": [origin.lat, origin.lon],
            "destination": [dest[0], dest[1]],
            "departure": None,
            "vessel_class": None,
        },
        "get_hazard_alerts": {"bbox": bbox, "when": None},
        "get_productivity_history": {"bbox": bbox, "years": 10},
        "nearest_harbour": {**pos, "n": 3},
        "evaluate_verdict": {**pos, "vessel_class": None, "when": None},
        "list_available_data": {},
    }


def warm_tools(region: RegionConfig, origin: Port, dest: tuple[float, float]) -> list[WarmRow]:
    """Call every tool in ``registry.all()`` with real demo arguments, isolated."""
    args_by_name = _tool_args_by_name(region, origin, dest)
    rows: list[WarmRow] = []
    for spec in registry.all():
        args = args_by_name.get(spec.name, {"lat": origin.lat, "lon": origin.lon})

        def _run(spec=spec, args=args) -> str:
            result = registry.call(spec.name, args)
            if not result.ok:
                raise RuntimeError(result.error or "tool returned ok=False")
            return (
                f"ok={result.ok} partial={result.partial} "
                f"observations={len(result.observations)} missing={result.missing}"
            )

        rows.append(_call_isolated(f"tool:{spec.name}", _run))
    return rows


# ------------------------------------------------------------------------------------
# Phase 1b — the three POST /api/query demo scenarios, run through the exact function
# routes_query.py's post_query calls (foreshore.agents.orchestrator.answer). This is
# equivalent to curling the endpoint minus the HTTP hop and a running uvicorn process —
# the goal is real cache files on disk, not a particular calling convention.
# ------------------------------------------------------------------------------------


def warm_demo_queries(origin: Port) -> list[WarmRow]:
    from foreshore.agents.orchestrator import Query, answer

    scenarios: list[tuple[str, Query]] = [
        (
            "query:safety_ta_boat",
            Query(text=SAFETY_QUERY_TA, lat=origin.lat, lon=origin.lon, surface="boat"),
        ),
        (
            "query:fishing_zone_console",
            Query(text=FISHING_ZONE_QUERY_EN, lat=origin.lat, lon=origin.lon, surface="console"),
        ),
        (
            "query:productivity_console",
            Query(text=PRODUCTIVITY_QUERY_EN, lat=origin.lat, lon=origin.lon, surface="console"),
        ),
    ]

    rows: list[WarmRow] = []
    for label, query in scenarios:
        def _run(query=query) -> str:
            outcome = answer(query)
            level = outcome.verdict.level if outcome.verdict else None
            return (
                f"verdict={level} tools_run={len(outcome.tool_results)} "
                f"missing={outcome.missing}"
            )

        rows.append(_call_isolated(label, _run))
    return rows


# ------------------------------------------------------------------------------------
# Phase 1c — the reference/fleet/health surface: GET /api/region, GET
# /api/geofences.geojson, GET /api/fleet, GET /health. Each calls exactly the function
# its route handler calls (see backend/foreshore/api/routes_reference.py,
# routes_fleet.py, main.py) — none of these need a running FastAPI app.
# ------------------------------------------------------------------------------------


def warm_reference_surface(region: RegionConfig) -> list[WarmRow]:
    rows: list[WarmRow] = []

    def _region() -> str:
        r = load_region(region.region_id)
        return f"region_id={r.region_id} anchor_ports={len(r.anchor_ports)}"

    rows.append(_call_isolated("api:region", _region))

    def _geofences() -> str:
        from foreshore.geofence.engine import GeofenceEngine

        fc = GeofenceEngine(region=region).as_geojson()
        return f"features={len(fc.get('features', []))}"

    rows.append(_call_isolated("api:geofences_geojson", _geofences))

    def _fleet() -> str:
        from foreshore.push.loop import PushLoop

        loop = PushLoop(region=region)
        # One real tick (not the background thread) so the dynamic HAZARD_EXCLUSION
        # fences actually get refreshed from get_exclusion_zones/GDACS, the same way a
        # server that has been running for a while would have already ticked by the
        # time a demo hits GET /api/fleet.
        loop.tick()
        vessels = loop.fleet_snapshot()
        return f"vessels={len(vessels)}"

    rows.append(_call_isolated("api:fleet", _fleet))

    def _health() -> str:
        from foreshore.tools.discovery import list_available_data

        result = list_available_data()
        return result.summary or "no summary"

    rows.append(_call_isolated("api:health", _health))

    return rows


# ------------------------------------------------------------------------------------
# Phase 2 — promote everything the warm pass just cached into data/fixtures/.
# ------------------------------------------------------------------------------------


@dataclass
class PromoteRow:
    source_id: str
    key: str
    kind: str  # "json" | "blob"
    bytes: int
    promoted: bool
    note: str = ""


def promote_json_snapshots() -> list[PromoteRow]:
    """Every ``<key>__latest.json`` snapshot under data/cache/ -> data/fixtures/, via
    the existing, unmodified ``promote_cache_to_fixture``. Reads ``source_id``/``key``
    back out of each record's own JSON rather than re-deriving them from the path, so
    this never depends on ``slugify`` being invertible."""
    import json as _json

    rows: list[PromoteRow] = []
    if not CACHE_DIR.exists():
        return rows
    for path in sorted(CACHE_DIR.rglob("*__latest.json")):
        try:
            rec = _json.loads(path.read_text(encoding="utf-8"))
            source_id = rec["source_id"]
            key = rec["key"]
        except Exception as exc:  # noqa: BLE001 - one unreadable snapshot must not stop the rest
            rows.append(PromoteRow(
                source_id="?", key=path.stem, kind="json", bytes=0, promoted=False,
                note=f"could not read latest snapshot {path}: {type(exc).__name__}: {exc}",
            ))
            continue
        try:
            promoted_path = cache_store.promote_cache_to_fixture(source_id, key)
        except Exception as exc:  # noqa: BLE001
            rows.append(PromoteRow(
                source_id=source_id, key=key, kind="json", bytes=0, promoted=False,
                note=f"promote failed: {type(exc).__name__}: {exc}",
            ))
            continue
        size = promoted_path.stat().st_size if promoted_path is not None else 0
        rows.append(PromoteRow(
            source_id=source_id, key=key, kind="json", bytes=size,
            promoted=promoted_path is not None,
            note="" if promoted_path is not None else "no live cache entry found",
        ))
    return rows


def promote_binary_blobs() -> list[PromoteRow]:
    """INCOIS OSF grid subsets bypass write_snapshot entirely (see store/cache.py's
    cache_binary/binary_path docstring — they are too large for the JSON record and
    carry no "latest" pointer, just a content-addressed key), so there is no
    promote_cache_to_fixture equivalent to call. This is the smallest correct thing
    that closes that gap: mirror every data/cache/<source>/blobs/<key>.nc file into the
    identical path under data/fixtures/ — a plain, deterministic file copy, since a
    blob's key already makes it the one canonical file for that exact request."""
    rows: list[PromoteRow] = []
    if not CACHE_DIR.exists():
        return rows
    for path in sorted(CACHE_DIR.rglob("blobs/*")):
        if not path.is_file():
            continue
        rel = path.relative_to(CACHE_DIR)
        source_id = rel.parts[0] if rel.parts else "?"
        dest = FIXTURE_DIR / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        except Exception as exc:  # noqa: BLE001
            rows.append(PromoteRow(
                source_id=source_id, key=path.stem, kind="blob", bytes=0, promoted=False,
                note=f"copy failed: {type(exc).__name__}: {exc}",
            ))
            continue
        rows.append(PromoteRow(
            source_id=source_id, key=path.stem, kind="blob", bytes=dest.stat().st_size, promoted=True,
        ))
    return rows


# ------------------------------------------------------------------------------------
# Reporting
# ------------------------------------------------------------------------------------


def _print_warm_table(title: str, rows: list[WarmRow]) -> None:
    name_w = max([len("label")] + [len(r.label) for r in rows]) + 2
    status_w = 8
    elapsed_w = 10
    print(f"\n-- {title} --")
    header = f"{'label':<{name_w}}{'status':<{status_w}}{'ms':>{elapsed_w}}  note"
    print(header)
    print("-" * len(header))
    for r in rows:
        status = "OK" if r.ok else "FAIL"
        print(f"{r.label:<{name_w}}{status:<{status_w}}{r.elapsed_ms:>{elapsed_w}}  {r.note}")
    ok_count = sum(1 for r in rows if r.ok)
    print(f"{ok_count}/{len(rows)} OK")


def _print_promote_table(rows: list[PromoteRow]) -> None:
    print("\n-- promoted to data/fixtures/ --")
    headers = ("source_id", "key", "kind", "bytes", "promoted")
    widths = [24, 40, 6, 10, 9]
    header_line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(header_line)
    print("-" * len(header_line))
    total_bytes = 0
    promoted_count = 0
    for r in sorted(rows, key=lambda r: (r.source_id, r.key)):
        cells = [r.source_id, r.key, r.kind, str(r.bytes), "y" if r.promoted else "n"]
        print("  ".join(c.ljust(w) for c, w in zip(cells, widths)))
        if r.note:
            print(f"    -> {r.note}")
        if r.promoted:
            total_bytes += r.bytes
            promoted_count += 1
    print("-" * len(header_line))
    print(f"{promoted_count}/{len(rows)} promoted, {total_bytes:,} bytes total")

    print("\n-- by source_id --")
    by_source: dict[str, tuple[int, int]] = {}
    for r in rows:
        if not r.promoted:
            continue
        count, size = by_source.get(r.source_id, (0, 0))
        by_source[r.source_id] = (count + 1, size + r.bytes)
    for source_id in sorted(by_source):
        count, size = by_source[source_id]
        print(f"  {source_id:<28}{count:>4} file(s)  {size:>12,} bytes")


# ------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Freeze live source snapshots into data/fixtures/ for the network-off demo."
    )
    parser.add_argument("--region", default=None, help="region id (default: $FORESHORE_REGION or palk_bay_gom)")
    args = parser.parse_args(argv)

    region = load_region(args.region)
    origin = region.anchor_ports[0]
    # A fishing ground south-east of Rameswaram, inside the region bbox
    # [78.0, 8.0, 80.6, 10.9] — PLAN.md's own suggested routing-demo destination.
    dest = (9.0, 79.8)
    if not region.contains(*dest):
        # A region swap (e.g. gujarat_sir_creek) could put this literal point outside
        # its bbox; fall back to a point offset from the origin instead of crashing.
        minlon, minlat, maxlon, maxlat = region.bbox
        dest = (minlat + (maxlat - minlat) * 0.3, minlon + (maxlon - minlon) * 0.7)

    print(f"FORESHORE fixture freeze — region={region.region_id} ({region.display_name_en})")
    print(f"FORESHORE_MODE=live (forced); origin={origin.name} ({origin.lat}, {origin.lon}); "
          f"route destination=({dest[0]:.4f}, {dest[1]:.4f})")
    if failed_modules():
        print(f"WARNING: tool modules failed to import: {failed_modules()}", file=sys.stderr)

    print(f"\n{len(registry.all())} tool(s) registered; warming every one directly...")
    tool_rows = warm_tools(region, origin, dest)
    _print_warm_table("tool registry sweep", tool_rows)

    print("\nrunning the three demo POST /api/query scenarios end to end...")
    query_rows = warm_demo_queries(origin)
    _print_warm_table("demo queries (orchestrator.answer)", query_rows)

    print("\nwarming the reference/fleet/health surface...")
    reference_rows = warm_reference_surface(region)
    _print_warm_table("reference / fleet / health", reference_rows)

    print("\npromoting data/cache/ -> data/fixtures/ ...")
    promote_rows = promote_json_snapshots() + promote_binary_blobs()
    _print_promote_table(promote_rows)

    total_warm = len(tool_rows) + len(query_rows) + len(reference_rows)
    ok_warm = sum(1 for r in (tool_rows + query_rows + reference_rows) if r.ok)
    promoted = sum(1 for r in promote_rows if r.promoted)
    print(
        f"\nSummary: {ok_warm}/{total_warm} warm steps OK; {promoted}/{len(promote_rows)} "
        f"snapshots promoted into {FIXTURE_DIR}."
    )
    # Exit 0 regardless of individual source/query outcomes -- see module docstring: an
    # honest failure or an empty-but-valid result (e.g. 0 active cyclones) is itself
    # real data worth freezing, per CLAUDE.md's own "0 features ... is valid, not an
    # error" note. This script only ever fails hard on a setup problem (bad --region),
    # which argparse/load_region already raise before this point.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
