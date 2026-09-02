"""Scheduled-ingestion CLI: refresh ``data/cache/`` from every live source.

Meant to be invoked periodically by a scheduler (a cron entry, or similar) — this file
contains only the one-shot script such a scheduler would call; it wires no actual
scheduling of its own.

Distinct from ``scripts/freeze_fixtures.py``, which is a separate, one-shot
demo-fixture-*promotion* tool: that script takes an existing cache/live snapshot and
promotes it into the committed ``data/fixtures/`` tree so a network-free demo/test run
can replay it. This script never writes to ``data/fixtures/`` — its only job is to keep
``data/cache/`` current for whatever the live agent request path reads next.

Forces ``FORESHORE_MODE=live`` at the very top of the module, before any ``foreshore``
import — mirrors ``scripts/healthcheck.py``, which does the same for the same reason:
this script's whole purpose is to dial real endpoints regardless of the calling shell's
env, and a bare ``import scripts.ingest`` inside the test suite must never flip
``backend/tests/conftest.py``'s session-wide ``FORESHORE_MODE=fixture`` back to live.

For each of the nine source adapters under ``backend/foreshore/sources/`` this calls the
adapter's own real, representative data-fetch method(s) — never just ``.health()`` — so
that a successful live call's normal cache-snapshot side effect
(``backend/foreshore/sources/base.py``'s module docstring: "every successful live fetch
is snapshotted") is what actually refreshes ``data/cache/``. Each source is isolated in
its own try/except (mirroring ``scripts/fetch_static.py``'s ``run_layer`` and
``scripts/healthcheck.py``'s ``_check``) so one dead endpoint can never stop the other
eight from refreshing. A single-digit number of failures among nine sources is a normal,
tolerable outcome for a scheduled job: this script only exits non-zero when *every*
source failed.

CLI
---
    python scripts/ingest.py [--sources imd_coastal_bulletin,incois_wfs,...]

Exit code 0 unless every source failed (exit 1 in that case).
"""

from __future__ import annotations

import os

# Force live mode before any `foreshore` import — this script's whole purpose is to hit
# real endpoints, regardless of the calling shell's env. Mirrors scripts/healthcheck.py
# (and, in reverse, backend/tests/conftest.py forcing FORESHORE_MODE=fixture for tests).
os.environ["FORESHORE_MODE"] = "live"

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Make `import foreshore` work whether or not the package is installed editable into the
# active venv — same defensive sys.path insertion scripts/healthcheck.py and
# scripts/fetch_static.py both use, so this script works from a bare checkout too.
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from foreshore.config import RegionConfig, load_region  # noqa: E402


@dataclass(frozen=True)
class IngestResult:
    """One source's result: a name, pass/fail, elapsed time, and a one-line note."""

    name: str
    ok: bool
    elapsed_ms: int
    note: str


# ------------------------------------------------------------------------------------
# Per-source ingest calls. Each does its own `foreshore.sources...` import lazily,
# inside the function body — never at module scope — so one adapter's import error
# degrades to that one source reporting FAILED rather than crashing this script before
# it can refresh the other eight. Same defensive-lazy-import discipline
# scripts/healthcheck.py and scripts/fetch_static.py both use for exactly this reason.
#
# Every function below calls a *real* data-fetch method (never just `.health()`) so the
# adapter's own `Source.get()` performs an actual live request and — on success —
# snapshots the payload into `data/cache/<source_id>/...`, which is this script's whole
# point.
# ------------------------------------------------------------------------------------


def _ingest_imd_bulletin(region: RegionConfig) -> tuple[int, str]:
    from foreshore.sources.imd_bulletin import IMDCoastalBulletin

    src = IMDCoastalBulletin(region=region)
    bulletin = src.bulletin()
    return 1, f"coast_block={bulletin.coast_block!r} issued_at={bulletin.issued_at}"


def _ingest_imd_geoserver(region: RegionConfig) -> tuple[int, str]:
    """Three independent WFS layers behind one adapter. Each is fetched in its own
    nested try/except so a dead nowcast layer, say, does not prevent the AWS and
    cyclone-track layers from still refreshing their own cache snapshots — mirrors the
    per-layer isolation `IMDGeoServer.health()` already applies for the same reason."""
    from foreshore.sources.imd_geoserver import IMDGeoServer

    src = IMDGeoServer(region=region)
    total = 0
    bits: list[str] = []
    any_ok = False
    for label, fn in (
        ("nowcast", src.nowcast_warnings),
        ("aws", src.aws_observations),
        ("cyclone_track", src.cyclone_track),
    ):
        try:
            feats, _raw = fn()
            total += len(feats)
            bits.append(f"{label}={len(feats)}")
            any_ok = True
        except Exception as exc:  # noqa: BLE001 - one layer failing must not sink the rest
            bits.append(f"{label}=FAIL({type(exc).__name__})")
    if not any_ok:
        raise RuntimeError(f"every imd_geoserver layer failed: {'; '.join(bits)}")
    return total, "; ".join(bits)


def _ingest_incois_wfs(region: RegionConfig) -> tuple[int, str]:
    """All eight INCOIS GeoServer workspaces this adapter knows, each isolated —
    mirrors ``IncoisWFS.health()``'s own per-layer probing (``eco_mangrove`` is
    documented there as intermittently flaky)."""
    from foreshore.sources.incois_wfs import IncoisWFS

    src = IncoisWFS(region=region)
    total = 0
    bits: list[str] = []
    any_ok = False
    layer_calls: tuple[tuple[str, Callable[[], tuple[list[Any], Any]]], ...] = (
        ("pfz_lines", src.pfz_lines),
        ("pfz_sectors", src.pfz_sectors),
        ("landing_centres", src.landing_centres),
        ("eco_coral", lambda: src.eco_zones("coral")),
        ("eco_seagrass", lambda: src.eco_zones("seagrass")),
        ("eco_mangrove", lambda: src.eco_zones("mangrove")),
        ("hab_sectors", src.hab_sectors),
        ("bathymetry", src.bathymetry),
    )
    for label, fn in layer_calls:
        try:
            feats, _raw = fn()
            total += len(feats)
            bits.append(f"{label}={len(feats)}")
            any_ok = True
        except Exception as exc:  # noqa: BLE001
            bits.append(f"{label}=FAIL({type(exc).__name__})")
    if not any_ok:
        raise RuntimeError(f"every incois_wfs layer failed: {'; '.join(bits)}")
    return total, "; ".join(bits)


def _ingest_incois_thredds(region: RegionConfig) -> tuple[int, str]:
    """One representative point read per OSF product (wave/mwh/currents/winds/sst/chl)
    at the region centre — each product is a distinct THREDDS dataset/cache key, so
    each is isolated the same way the multi-layer adapters above are."""
    from foreshore.sources.incois_thredds import PRODUCTS, IncoisThredds

    src = IncoisThredds(region=region)
    lat, lon = region.centre
    total = 0
    bits: list[str] = []
    any_ok = False
    for product in PRODUCTS:
        try:
            obs = src.point(product, lat, lon)
            total += len(obs)
            bits.append(f"{product}={len(obs)}")
            any_ok = True
        except Exception as exc:  # noqa: BLE001
            bits.append(f"{product}=FAIL({type(exc).__name__})")
    if not any_ok:
        raise RuntimeError(f"every incois_osf product failed: {'; '.join(bits)}")
    return total, "; ".join(bits)


def _ingest_incois_argo(region: RegionConfig) -> tuple[int, str]:
    from foreshore.sources.incois_erddap import IncoisArgo

    src = IncoisArgo(region=region)
    lat, lon = region.centre
    obs = src.profile(lat, lon)
    return len(obs), f"profile at region centre ({lat:.2f},{lon:.2f}): {len(obs)} observation(s)"


def _ingest_openmeteo_marine(region: RegionConfig) -> tuple[int, str]:
    from foreshore.sources.openmeteo import OpenMeteoMarine

    src = OpenMeteoMarine(region=region)
    lat, lon = region.centre
    obs = src.series(lat, lon, hours=48)
    return len(obs), f"48h marine series at region centre: {len(obs)} observation(s)"


def _ingest_openmeteo_forecast(region: RegionConfig) -> tuple[int, str]:
    from foreshore.sources.openmeteo import OpenMeteoForecast

    src = OpenMeteoForecast(region=region)
    lat, lon = region.centre
    obs = src.series(lat, lon, hours=48)
    return len(obs), f"48h atmospheric series at region centre: {len(obs)} observation(s)"


def _ingest_gdacs(region: RegionConfig) -> tuple[int, str]:
    from foreshore.sources.gdacs import GDACSCyclones

    src = GDACSCyclones(region=region)
    events, _raw = src.events(current_only=True)
    # 0 current cyclones globally is a valid, common outcome (per CLAUDE.md) — never an
    # error, exactly as scripts/healthcheck.py treats it.
    return len(events), f"{len(events)} current TC event(s) globally"


def _ingest_marine_regions(region: RegionConfig) -> tuple[int, str]:
    from foreshore.sources.marine_regions import MarineRegionsIMBL

    src = MarineRegionsIMBL(region=region)
    segs, _raw = src.segments()
    return len(segs), f"{len(segs)} treaty boundary segment(s)"


#: (report name, ingest function) — one row per source adapter file under
#: backend/foreshore/sources/, matching the nine adapters this task names.
SOURCES: list[tuple[str, Callable[[RegionConfig], tuple[int, str]]]] = [
    ("imd_coastal_bulletin", _ingest_imd_bulletin),
    ("imd_geoserver", _ingest_imd_geoserver),
    ("incois_wfs", _ingest_incois_wfs),
    ("incois_thredds", _ingest_incois_thredds),
    ("incois_argo", _ingest_incois_argo),
    ("openmeteo_marine", _ingest_openmeteo_marine),
    ("openmeteo_forecast", _ingest_openmeteo_forecast),
    ("gdacs", _ingest_gdacs),
    ("marine_regions_imbl", _ingest_marine_regions),
]


def _run_one(name: str, fn: Callable[[], tuple[int, str]]) -> IngestResult:
    """Run one source's ingest call, translating any exception into a FAILED row.

    A failure here is deliberately swallowed at this level (never re-raised) — one dead
    source must never take the rest of the run down with it.
    """
    t0 = time.perf_counter()
    try:
        count, note = fn()
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return IngestResult(name=name, ok=True, elapsed_ms=elapsed_ms, note=f"count={count}; {note}")
    except Exception as exc:  # noqa: BLE001 - a source failing must not abort the run
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return IngestResult(
            name=name, ok=False, elapsed_ms=elapsed_ms, note=f"{type(exc).__name__}: {exc}",
        )


def run_ingest(
    region: RegionConfig | None = None, wanted: list[str] | None = None
) -> list[IngestResult]:
    """Thin loop: call every selected source's ingest function and collect the results.

    All the actual network I/O happens inside the nine lazily-imported functions above,
    via each adapter's own ``Source.get`` — nothing in this function does its own HTTP.
    """
    region = region or load_region()
    sources = SOURCES if not wanted else [(n, f) for n, f in SOURCES if n in set(wanted)]
    return [_run_one(name, (lambda f=fn: f(region))) for name, fn in sources]


def summarise(results: list[IngestResult]) -> tuple[str, int]:
    """Pure, network-free: render a summary table, and the exit code.

    Exit code is 0 unless *every* given :class:`IngestResult` failed (a single-digit
    number of failures among nine sources is a normal, tolerable outcome for a
    scheduled job — this is intentionally more forgiving than
    ``scripts/healthcheck.py``'s "any failure -> exit 1", which exists to flag drift for
    a human reading a morning report, not to gate an unattended cron run).
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

    # Vacuous case (no sources selected/run) is treated as OK, same as
    # scripts/healthcheck.py's format_report — there is nothing to have failed.
    exit_code = 0 if (total == 0 or ok_count > 0) else 1
    return "\n".join(lines), exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh data/cache/ from every live FORESHORE source adapter."
    )
    parser.add_argument("--region", default=None, help="region id (default: $FORESHORE_REGION or palk_bay_gom)")
    parser.add_argument(
        "--sources", default=None,
        help="comma-separated subset of source names to ingest (default: all nine)",
    )
    args = parser.parse_args(argv)

    region = load_region(args.region)
    wanted = [s.strip() for s in args.sources.split(",")] if args.sources else None

    print(
        f"FORESHORE scheduled ingest — region={region.region_id} "
        f"({region.display_name_en}); FORESHORE_MODE=live (forced)"
    )
    print()

    results = run_ingest(region=region, wanted=wanted)
    text, exit_code = summarise(results)
    print(text)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
