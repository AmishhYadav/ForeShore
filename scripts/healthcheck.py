"""Daily operational healthcheck for every FORESHORE live source adapter.

CLAUDE.md says: "Re-run scripts/healthcheck.py each morning — operational endpoints
move." This is that script. Operational endpoints in the "Data sources — verified live"
table are volatile (INCOIS/IMD GeoServer workspaces, THREDDS catalogue paths, ERDDAP
dataset ids) in a way this repo's frozen fixtures under ``data/fixtures/`` can never
surface — a fixture replays what a source *used* to return. This script is the only
thing in the repo that actually dials every one of the eight live source adapters and
reports, this morning, whether each one is still reachable and shaped the way the
adapter code expects.

Forces ``FORESHORE_MODE=live`` at the very top of the module, before any ``foreshore``
import — this script's whole purpose is to hit real endpoints regardless of the calling
shell's env, mirroring (in reverse) how ``backend/tests/conftest.py`` forces
``FORESHORE_MODE=fixture`` at import time for the whole test session so no test ever
opens a socket.

For each of the eight adapters under ``backend/foreshore/sources/``, this calls the
adapter's own :meth:`~foreshore.sources.base.Source.health` — the hook
``Source.health`` documents in its own docstring as *"Used by scripts/healthcheck.py"* —
which already performs one minimal, representative live read of that source (the base
default: a single ``fetch()``/``parse()`` pair; several adapters override it with a
richer multi-layer probe, e.g. :class:`~foreshore.sources.imd_geoserver.IMDGeoServer`
covers its three WFS layers, :class:`~foreshore.sources.incois_wfs.IncoisWFS` covers all
eight of its GeoServer workspaces). This script never re-implements URL/header
construction or a second notion of "minimal call" — it reuses exactly what the adapter
already considers its own health probe, the same way ``scripts/fetch_static.py`` reuses
each adapter's own typed fetchers rather than hand-rolling requests.

Classification is purely on whether the call itself succeeded: ``health()`` already
treats a reachable-but-empty result (0 active cyclones, 0 PFZ lines issued today, no
cyclone track points) as ``ok=True`` per CLAUDE.md's own note that "0 features when no
active cyclone" is valid, not an error — this script does not second-guess that by
looking at counts itself. One source's import failure or exception can never abort the
run for the other seven: each check is isolated (mirrors ``fetch_static.py``'s
``run_layer``, which swallows a layer's exception rather than letting it kill the plan).

This script only reads live and prints a report; it never writes to ``data/cache/`` or
touches a committed fixture (writing snapshots to the cache on a successful live fetch
is ``Source.get``'s own existing behaviour, unchanged and out of scope here — this
script adds no new writes of its own).

CLI
---
    python scripts/healthcheck.py

Exit code 0 if every source is OK, 1 if any FAILED.
"""

from __future__ import annotations

import os

# Force live mode before any `foreshore` import — this script's whole purpose is to hit
# real endpoints, regardless of the calling shell's env. Mirrors, in reverse, how
# backend/tests/conftest.py forces FORESHORE_MODE=fixture at import time for every test.
os.environ["FORESHORE_MODE"] = "live"

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Make `import foreshore` work whether or not the package is installed editable into the
# active venv — same defensive sys.path insertion scripts/fetch_static.py uses, so this
# script works from a bare checkout too.
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from foreshore.config import RegionConfig, load_region  # noqa: E402


@dataclass(frozen=True)
class SourceCheck:
    """One source's result: a name, pass/fail, elapsed time, and a one-line note."""

    name: str
    ok: bool
    elapsed_ms: int
    note: str


# ------------------------------------------------------------------------------------
# Per-source probes. Each does its own `foreshore.sources...` import lazily, inside the
# function body — never at module scope — so a single adapter's import error (a missing
# optional dependency, a syntax error introduced elsewhere) degrades to that one source
# reporting FAILED rather than crashing this script before it can report on the other
# seven. This is the same defensive-lazy-import discipline scripts/fetch_static.py uses
# for exactly the same reason (see its `_imbl_segments` docstring).
# ------------------------------------------------------------------------------------


def _imd_bulletin_health(region: RegionConfig) -> dict[str, Any]:
    from foreshore.sources.imd_bulletin import IMDCoastalBulletin

    return IMDCoastalBulletin(region=region).health()


def _imd_geoserver_health(region: RegionConfig) -> dict[str, Any]:
    from foreshore.sources.imd_geoserver import IMDGeoServer

    return IMDGeoServer(region=region).health()


def _incois_wfs_health(region: RegionConfig) -> dict[str, Any]:
    from foreshore.sources.incois_wfs import IncoisWFS

    return IncoisWFS(region=region).health()


def _incois_osf_health(region: RegionConfig) -> dict[str, Any]:
    from foreshore.sources.incois_thredds import IncoisThredds

    return IncoisThredds(region=region).health()


def _incois_argo_health(region: RegionConfig) -> dict[str, Any]:
    from foreshore.sources.incois_erddap import IncoisArgo

    return IncoisArgo(region=region).health()


def _openmeteo_health(region: RegionConfig) -> dict[str, Any]:
    """Covers both Open-Meteo endpoints (marine + atmospheric forecast) — they live in
    one adapter module (``openmeteo.py``) but are two distinct classes/URLs, so both are
    probed and combined into a single row, the same way IMDGeoServer.health() combines
    its three WFS layers into one row rather than reporting them separately."""
    from foreshore.sources.openmeteo import OpenMeteoForecast, OpenMeteoMarine

    marine = OpenMeteoMarine(region=region).health()
    forecast = OpenMeteoForecast(region=region).health()
    ok = bool(marine.get("ok")) and bool(forecast.get("ok"))
    errors = [e for e in (marine.get("error"), forecast.get("error")) if e]
    return {
        "ok": ok,
        "count": (marine.get("count") or 0) + (forecast.get("count") or 0),
        "issued_at": marine.get("issued_at") or forecast.get("issued_at"),
        "error": "; ".join(errors) or None,
    }


def _gdacs_health(region: RegionConfig) -> dict[str, Any]:
    from foreshore.sources.gdacs import GDACSCyclones

    return GDACSCyclones(region=region).health()


def _marine_regions_health(region: RegionConfig) -> dict[str, Any]:
    from foreshore.sources.marine_regions import MarineRegionsIMBL

    return MarineRegionsIMBL(region=region).health()


#: (report name, probe function) — one row per source adapter file under
#: backend/foreshore/sources/, in the order CLAUDE.md's "Data sources — verified live"
#: table roughly introduces them.
CHECKS: list[tuple[str, Callable[[RegionConfig], dict[str, Any]]]] = [
    ("imd_coastal_bulletin", _imd_bulletin_health),
    ("imd_geoserver", _imd_geoserver_health),
    ("incois_wfs", _incois_wfs_health),
    ("incois_osf", _incois_osf_health),
    ("incois_argo", _incois_argo_health),
    ("openmeteo", _openmeteo_health),
    ("gdacs_tc", _gdacs_health),
    ("marine_regions_imbl", _marine_regions_health),
]


def _summarise(info: dict[str, Any]) -> str:
    """One-line shape summary for a successful check: count plus a key field or two."""
    bits = [f"count={info.get('count')}"]
    if info.get("issued_at"):
        bits.append(f"issued_at={info['issued_at']}")
    if info.get("freshness"):
        bits.append(f"freshness={info['freshness']}")
    return "; ".join(bits)


def _check(name: str, fn: Callable[[], dict[str, Any]]) -> SourceCheck:
    """Run one probe, translating any exception into a FAILED row.

    A failure here is deliberately swallowed at this level (never re-raised) — one dead
    source must never take the rest of the run down with it, exactly the contract
    ``scripts/fetch_static.py``'s ``run_layer`` enforces for static layers.
    """
    t0 = time.perf_counter()
    try:
        info = fn()
    except Exception as exc:  # noqa: BLE001 - a source failing must not abort the run
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return SourceCheck(
            name=name, ok=False, elapsed_ms=elapsed_ms, note=f"{type(exc).__name__}: {exc}",
        )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    ok = bool(info.get("ok"))
    note = _summarise(info) if ok else (info.get("error") or "health() returned ok=False, no error detail")
    return SourceCheck(name=name, ok=ok, elapsed_ms=elapsed_ms, note=note)


def run_checks(region: RegionConfig | None = None) -> list[SourceCheck]:
    """Thin loop: call every adapter's health probe and collect the results.

    All the actual network I/O happens here (inside the eight lazily-imported probe
    functions above via each adapter's own ``Source.get``/``health()``). Nothing in this
    function does its own HTTP — it only times and isolates each call.
    """
    region = region or load_region()
    return [_check(name, (lambda f=fn: f(region))) for name, fn in CHECKS]


def format_report(results: list[SourceCheck]) -> tuple[str, int]:
    """Pure, network-free: render a summary table + verdict line, and the exit code.

    Exit code is 0 only if every given :class:`SourceCheck` is ``ok``; 1 if any FAILED.
    """
    name_w = max([len("source")] + [len(r.name) for r in results]) + 2
    status_w = 8
    elapsed_w = 12

    lines: list[str] = []
    header = f"{'source':<{name_w}}{'status':<{status_w}}{'elapsed_ms':>{elapsed_w}}  note"
    lines.append(header)
    lines.append("-" * len(header))

    ok_count = 0
    for r in results:
        status = "OK" if r.ok else "FAIL"
        if r.ok:
            ok_count += 1
        lines.append(f"{r.name:<{name_w}}{status:<{status_w}}{r.elapsed_ms:>{elapsed_w}}  {r.note}")

    total = len(results)
    lines.append("")
    lines.append(f"{ok_count}/{total} sources OK")

    exit_code = 0 if ok_count == total else 1
    return "\n".join(lines), exit_code


def main(argv: list[str] | None = None) -> int:
    region = load_region()
    print(
        f"FORESHORE source healthcheck — region={region.region_id} "
        f"({region.display_name_en}); FORESHORE_MODE=live (forced)"
    )
    print()

    results = run_checks(region=region)
    text, exit_code = format_report(results)
    print(text)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
