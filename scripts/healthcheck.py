"""Daily operational healthcheck for every FORESHORE live source adapter.

CLAUDE.md says: "Re-run scripts/healthcheck.py each morning — operational endpoints
move." This is that script. Operational endpoints in the "Data sources — verified live"
table are volatile (INCOIS/IMD GeoServer workspaces, THREDDS catalogue paths, ERDDAP
dataset ids) in a way this repo's frozen fixtures under ``data/fixtures/`` can never
surface — a fixture replays what a source *used* to return. This script is the only
thing in the repo that actually dials every live source adapter and reports, this
morning, whether each one is still reachable and shaped the way the adapter code
expects.

Defaults to ``FORESHORE_MODE=live`` at the very top of the module, before any
``foreshore`` import — this script's default purpose is to hit real endpoints, mirroring
(in reverse) how ``backend/tests/conftest.py`` forces ``FORESHORE_MODE=fixture`` at
import time for the whole test session so no test ever opens a socket. Unlike that
conftest, this default is not absolute: an explicit ``FORESHORE_MODE=fixture`` in the
calling shell is honoured rather than overridden, so ``FORESHORE_MODE=fixture python
scripts/healthcheck.py`` runs the exact same probes entirely against
``data/fixtures/`` with no socket opened (``sources/base.py`` already guarantees that in
fixture mode) — this is PLAN.md's own stated Phase 1 acceptance criterion ("healthcheck.py
shows all-green live; FORESHORE_MODE=fixture healthcheck.py all-green with the network
off") and CLAUDE.md's Phase 8 network-off rehearsal, not a separate code path invented
here. Bare ``python scripts/healthcheck.py`` with no env var set still defaults to live,
unchanged, because that is this script's daily-morning purpose per CLAUDE.md.

For each adapter under ``backend/foreshore/sources/``, this calls the adapter's own
:meth:`~foreshore.sources.base.Source.health` — the hook ``Source.health`` documents in
its own docstring as *"Used by scripts/healthcheck.py"* — which already performs one
minimal, representative live read of that source (the base default: a single
``fetch()``/``parse()`` pair; several adapters override it with a richer multi-layer
probe, e.g. :class:`~foreshore.sources.imd_geoserver.IMDGeoServer` covers its three WFS
layers, :class:`~foreshore.sources.incois_wfs.IncoisWFS` covers all eight of its
GeoServer workspaces, :class:`~foreshore.sources.oceancolour.OceanColour` covers three
NOAA CoastWatch datasets). This script never re-implements URL/header construction or a
second notion of "minimal call" — it reuses exactly what the adapter already considers
its own health probe, the same way ``scripts/fetch_static.py`` reuses each adapter's own
typed fetchers rather than hand-rolling requests.

**Reachability is not usability.** ``health()`` already treats a reachable-but-empty
result (0 active cyclones, 0 PFZ lines issued today, no cyclone track points) as
``ok=True`` per CLAUDE.md's own note that "0 features when no active cyclone" is valid,
not an error — this script does not second-guess *that* by looking at counts itself. But
two sources answer every morning while being unable to support the claim FORESHORE makes
with them, and neither is a transport failure ``health()`` can see:

* **PFZ line currency.** ``PFZ_Automation:pfzlines`` can answer ``200`` with real
  features whose ``Year``/``Julian_day`` are years old — the official advisory line has
  sat frozen at 2021-09-05 while this script kept reporting ``incois_wfs`` reachable.
* **Grid coverage.** INCOIS OSF's ``chl`` product answers normally with a real NetCDF
  grid that is centred on the Pacific Islands (``lat -25.979..18.021, lon
  129.979..215.021``) and has never once intersected this system's Indian Ocean bbox.

A third row (:class:`~foreshore.sources.incois_erddap.IncoisOceansat`) answers with
every value already stamped ``freshness="expired"`` **by design** — a closed 2011-2020
mission archive, not a live feed — and must not be confused with the first two: this
script asks it whether it is what it claims to be (a closed archive) rather than whether
it is current.

So this script adds a third state, ``STALE``, sitting between ``OK`` and ``FAIL``:
reachable, but the *content* cannot support the claim the system makes with it. The two
live checks that can produce it are pure functions of already-fetched data
(:func:`_pfz_currency_verdict`, :func:`_grid_coverage_verdict`) — unit-tested directly in
``backend/tests/test_healthcheck.py`` with no network, the same way :func:`format_report`
already is. Everything else keeps the old reachable-is-``OK`` rule unchanged.

One source's import failure or exception can never abort the run for the others: each
check is isolated (mirrors ``fetch_static.py``'s ``run_layer``, which swallows a layer's
exception rather than letting it kill the plan).

This script only reads live and prints a report; it never writes to ``data/cache/`` or
touches a committed fixture (writing snapshots to the cache on a successful live fetch
is ``Source.get``'s own existing behaviour, unchanged and out of scope here — this
script adds no new writes of its own).

CLI
---
    python scripts/healthcheck.py                       # live (default)
    FORESHORE_MODE=fixture python scripts/healthcheck.py  # network-off, against data/fixtures/

Exit code is non-zero if any row is ``FAIL`` **or** ``STALE`` — see the comment on that
decision at :func:`format_report`. 0 only if every row is ``OK``.
"""

from __future__ import annotations

import os

# Default to live mode before any `foreshore` import, but honour an explicit
# FORESHORE_MODE=fixture from the calling shell rather than clobbering it — see the
# module docstring. This is the one deliberate difference from backend/tests/conftest.py,
# which forces fixture mode unconditionally: that file's job is "never let a test open a
# socket, ever"; this script's job is "hit real endpoints every morning by default, but
# still work as the FORESHORE_MODE=fixture network-off healthcheck PLAN.md promises."
if os.environ.get("FORESHORE_MODE", "").strip().lower() != "fixture":
    os.environ["FORESHORE_MODE"] = "live"

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Make `import foreshore` work whether or not the package is installed editable into the
# active venv — same defensive sys.path insertion scripts/fetch_static.py uses, so this
# script works from a bare checkout too.
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from foreshore.config import RegionConfig, load_region, mode  # noqa: E402


@dataclass(frozen=True)
class SourceCheck:
    """One source's result: a name, a tri-state status, elapsed time, and a one-line
    note.

    ``ok`` is kept, unchanged in meaning, alongside the new ``status`` field for
    backward compatibility with anything that only ever asked "did this source answer":
    it is ``True`` exactly when the underlying probe succeeded, ``STALE`` included —
    STALE content still came from a reachable source. ``status`` is the field that
    actually distinguishes the three outcomes and is what :func:`format_report` renders
    and counts against the exit code.
    """

    name: str
    ok: bool
    elapsed_ms: int
    note: str
    status: str = "OK"  # "OK" | "STALE" | "FAIL"


@dataclass(frozen=True)
class CheckSpec:
    """One source's row spec: a name, its ``health()`` probe, and an optional currency
    check layered on top of a *successful* probe (``currency`` is never called if
    ``probe`` itself failed — there is nothing to check the currency of).

    ``currency`` receives the region and the raw ``info`` dict the probe already
    returned, and answers ``None`` ("nothing to say — this source's currency is not
    separately tracked, or there was nothing to check") or ``(status, note)`` where
    ``status`` is ``"OK"`` or ``"STALE"``. It never raises the row above what ``probe``
    already established: a currency check can only downgrade ``OK`` to ``STALE``, the
    same one-directional rule ``verdict/ceiling.py`` applies to the advisory verdict
    itself.
    """

    name: str
    probe: Callable[[RegionConfig], dict[str, Any]]
    currency: Callable[[RegionConfig, dict[str, Any]], tuple[str, str] | None] | None = None


# ------------------------------------------------------------------------------------
# Per-source probes. Each does its own `foreshore.sources...` import lazily, inside the
# function body — never at module scope — so a single adapter's import error (a missing
# optional dependency, a syntax error introduced elsewhere) degrades to that one source
# reporting FAILED rather than crashing this script before it can report on the others.
# This is the same defensive-lazy-import discipline scripts/fetch_static.py uses for
# exactly the same reason (see its `_imbl_segments` docstring).
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


def _incois_oceansat_health(region: RegionConfig) -> dict[str, Any]:
    from foreshore.sources.incois_erddap import IncoisOceansat

    return IncoisOceansat(region=region).health()


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


# ------------------------------------------------------------------------------------
# Currency checks. The pure half of each (below) takes already-extracted data and does
# no I/O at all — these are what backend/tests/test_healthcheck.py extracts via `ast`,
# the same technique it already uses for `format_report`, so the STALE logic is tested
# without a socket and without a live FORESHORE_MODE=live import. The live-wiring half
# (further below, after CHECKS) does the one extra fetch or lookup each needs and hands
# the pure function real inputs.
# ------------------------------------------------------------------------------------


def _bbox_intersects(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    """Axis-aligned rectangle intersection test. Both boxes are ``(minlon, minlat,
    maxlon, maxlat)`` in EPSG:4326 — the same order as ``RegionConfig.bbox``. Pure, no
    I/O: the live wiring below is the only code in this file that ever calls
    ``load_region()`` for a bbox; this function never hardcodes one.
    """
    a_minlon, a_minlat, a_maxlon, a_maxlat = a
    b_minlon, b_minlat, b_maxlon, b_maxlat = b
    return a_minlon <= b_maxlon and b_minlon <= a_maxlon and a_minlat <= b_maxlat and b_minlat <= a_maxlat


def _grid_coverage_verdict(
    product_label: str,
    extent: tuple[float, float, float, float],
    region_bbox: tuple[float, float, float, float],
) -> tuple[str, str]:
    """OK/STALE verdict for one gridded product's own published extent against the
    active region's bbox. Pure — the caller supplies ``region_bbox`` (always
    ``region.bbox`` from ``load_region()`` in the live wiring, never a literal here), so
    this is unit-testable with plain tuples and no ``RegionConfig``.
    """
    if _bbox_intersects(extent, region_bbox):
        return "OK", f"{product_label} grid covers this region"
    return "STALE", (
        f"{product_label} grid extent {extent} does not intersect the region bbox "
        f"{region_bbox} — this product's data cannot support this region"
    )


def _pfz_currency_verdict(
    advisory_date: datetime, *, now: datetime, max_age_days: float
) -> tuple[str, str]:
    """OK/STALE verdict for one PFZ advisory date, against the source's own declared
    publication cadence — ``PFZ_ADVISORY_MAX_AGE_DAYS`` in
    ``backend/foreshore/tools/pfz.py`` (reused here rather than duplicated; see that
    constant's own comment for why 7 days is the source's cadence and not a guess). Pure
    — takes a plain ``datetime``, not a live fetch — so it is unit-testable without a
    socket.
    """
    age_days = (now - advisory_date).total_seconds() / 86400.0
    if age_days > max_age_days:
        return "STALE", (
            f"official PFZ advisory line is dated {advisory_date.date().isoformat()} "
            f"({age_days:.1f} days old) — past its {max_age_days:.0f}-day publication cadence"
        )
    return "OK", f"official PFZ advisory line is current (dated {advisory_date.date().isoformat()})"


#: (minlon, minlat, maxlon, maxlat) — INCOIS OSF chl's real published grid, confirmed
#: live on 2026-09-06 (see foreshore/sources/incois_thredds.py's module docstring: the
#: VIIRS-SNPP-Roll-*-4KM-PICountries-CHL.nc composite is Pacific-Islands-Countries
#: framed, not Indian-Ocean). INCOIS exposes no cheap metadata call for this bound — the
#: adapter only learns it by requesting a subset and getting an NCSS 400 — so, like every
#: other live-probed fact this repo already hardcodes with a citation (PRODUCTS in
#: incois_thredds.py, DATASETS in oceancolour.py), it is recorded here rather than
#: re-derived on every run. The live wiring below trusts an actual successful `chl` read
#: over this constant first, so a future INCOIS fix self-heals without anyone updating
#: it — see `_incois_osf_currency`.
_INCOIS_OSF_CHL_EXTENT: tuple[float, float, float, float] = (129.979, -25.979, 215.021, 18.021)


# ------------------------------------------------------------------------------------
# Currency checks, live-wiring half. Each does the one extra fetch/lookup its check
# needs (or none, for the pure grid-coverage comparison) and hands a real value to the
# pure function above.
# ------------------------------------------------------------------------------------


def _pfz_currency(region: RegionConfig, info: dict[str, Any]) -> tuple[str, str] | None:
    """Real advisory age for the PFZ line this region actually got today, read off the
    feature's own Year/Julian_day (see ``IncoisWFS._advisory_date`` /
    ``nearest_pfz_line`` — that parsing is not duplicated here). Skips the extra fetch
    entirely when the base probe's own ``pfz_lines`` layer count was already 0 or
    ``None``: nothing to check, and INCOIS not issuing a line every day is a documented
    valid outcome, not staleness.
    """
    layers = info.get("layers") or {}
    if not layers.get("pfz_lines"):
        return None
    from foreshore.sources.incois_wfs import IncoisWFS
    from foreshore.tools.pfz import PFZ_ADVISORY_MAX_AGE_DAYS

    wfs = IncoisWFS(region=region)
    lat, lon = region.centre
    found = wfs.nearest_pfz_line(lat, lon)
    if found is None:
        return None
    _obs, payload = found
    advisory_date_raw = payload.get("advisory_date")
    if not advisory_date_raw:
        return "STALE", (
            "official PFZ advisory line carries no parseable Year/Julian_day, so its "
            "currency cannot be confirmed"
        )
    advisory_date = datetime.fromisoformat(advisory_date_raw)
    from foreshore.models import utcnow

    return _pfz_currency_verdict(advisory_date, now=utcnow(), max_age_days=PFZ_ADVISORY_MAX_AGE_DAYS)


def _incois_osf_currency(region: RegionConfig, info: dict[str, Any]) -> tuple[str, str] | None:
    """Chlorophyll (``chl``) is the one OSF product with a known coverage gap — see
    :data:`_INCOIS_OSF_CHL_EXTENT`. A live success is trusted first: if INCOIS ever
    republishes ``chl`` with corrected coverage, the base probe's own per-product read
    (already performed by ``IncoisThredds.health()``, no extra fetch here) says so and
    this stops flagging STALE without anyone having to update the hardcoded extent.
    """
    chl_info = (info.get("products") or {}).get("chl") or {}
    if chl_info.get("ok"):
        return "OK", "INCOIS OSF chl returned live data for this region"
    return _grid_coverage_verdict(
        "INCOIS OSF chl (VIIRS PICountries composite)", _INCOIS_OSF_CHL_EXTENT, tuple(region.bbox)
    )


def _oceansat_currency(region: RegionConfig, info: dict[str, Any]) -> tuple[str, str] | None:
    """IncoisOceansat is a **closed** 2011-2020 mission archive by design — its own
    ``health()`` already stamps ``freshness="expired"`` on every value, forever, per its
    class docstring. That is correct, not staleness: this check exists solely so the age
    of a closed archive is never mistaken for the same failure mode as a five-year-stale
    *live* advisory. Never downgrades — only ever confirms and explains.
    """
    if info.get("archive_closed"):
        start = info.get("time_coverage_start")
        end = info.get("time_coverage_end")
        return "OK", f"closed archive ({start} to {end}) by design — not flagged stale for its age"
    return None


#: (report name, spec) — one row per source adapter file under
#: backend/foreshore/sources/, in the order CLAUDE.md's "Data sources — verified live"
#: table roughly introduces them. NOAA CoastWatch (`oceancolour.py`) is not here: its
#: `health()` fans out into several independent dataset rows, handled separately by
#: `_oceancolour_checks` and appended in `run_checks`.
CHECKS: list[CheckSpec] = [
    CheckSpec("imd_coastal_bulletin", _imd_bulletin_health),
    CheckSpec("imd_geoserver", _imd_geoserver_health),
    CheckSpec("incois_wfs", _incois_wfs_health, _pfz_currency),
    CheckSpec("incois_osf", _incois_osf_health, _incois_osf_currency),
    CheckSpec("incois_argo", _incois_argo_health),
    CheckSpec("incois_oceansat", _incois_oceansat_health, _oceansat_currency),
    CheckSpec("openmeteo", _openmeteo_health),
    CheckSpec("gdacs_tc", _gdacs_health),
    CheckSpec("marine_regions_imbl", _marine_regions_health),
]


def _summarise(info: dict[str, Any]) -> str:
    """One-line shape summary for a successful check: count plus a key field or two."""
    bits = [f"count={info.get('count')}"]
    if info.get("issued_at"):
        bits.append(f"issued_at={info['issued_at']}")
    if info.get("freshness"):
        bits.append(f"freshness={info['freshness']}")
    return "; ".join(bits)


def _check(spec: CheckSpec, region: RegionConfig) -> SourceCheck:
    """Run one probe, translating any exception into a FAIL row. On a successful probe,
    layer the spec's currency check (if any) on top — it can only downgrade the row's
    ``status`` from ``OK`` to ``STALE``, never raise it, and never turns a successful
    probe into a FAIL: a currency check that itself errors is reported as STALE (content
    currency could not be confirmed), not as a reason to hide the base result.
    """
    t0 = time.perf_counter()
    try:
        info = spec.probe(region)
    except Exception as exc:  # noqa: BLE001 - a source failing must not abort the run
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return SourceCheck(
            name=spec.name, ok=False, status="FAIL", elapsed_ms=elapsed_ms,
            note=f"{type(exc).__name__}: {exc}",
        )

    ok = bool(info.get("ok"))
    if not ok:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        note = info.get("error") or "health() returned ok=False, no error detail"
        return SourceCheck(name=spec.name, ok=False, status="FAIL", elapsed_ms=elapsed_ms, note=note)

    note = _summarise(info)
    status = "OK"
    if spec.currency is not None:
        try:
            verdict = spec.currency(region, info)
        except Exception as exc:  # noqa: BLE001 - a currency check failing must not hide the base result
            verdict = ("STALE", f"currency check failed: {type(exc).__name__}: {exc}")
        if verdict is not None:
            cur_status, cur_note = verdict
            status = cur_status
            note = f"{note}; {cur_note}"

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return SourceCheck(name=spec.name, ok=ok, status=status, elapsed_ms=elapsed_ms, note=note)


def _oceancolour_checks(region: RegionConfig) -> list[SourceCheck]:
    """One row per NOAA CoastWatch dataset (`OceanColour.health()`'s own ``"datasets"``
    list) — surfaced as separate rows rather than folded into one, unlike
    `_openmeteo_health`'s two-endpoint merge above. A human scanning this table for "is
    chlorophyll actually usable" needs to see the gap-filled VIIRS composite, MODIS and
    OISST as three independently-lagging products, because that is exactly the shape of
    failure `OceanColour.health()`'s own docstring calls out: "shows which of the three
    is lagging rather than a single OK that hides two dead products."
    """
    from foreshore.sources.oceancolour import OceanColour

    t0 = time.perf_counter()
    try:
        info = OceanColour(region=region).health()
    except Exception as exc:  # noqa: BLE001 - one broken adapter must not sink the run
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return [SourceCheck(
            name="oceancolour", ok=False, status="FAIL", elapsed_ms=elapsed_ms,
            note=f"{type(exc).__name__}: {exc}",
        )]

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    datasets = info.get("datasets") or []
    if not datasets:
        ok = bool(info.get("ok"))
        note = _summarise(info) if ok else (info.get("error") or "no datasets reported")
        return [SourceCheck(name="oceancolour", ok=ok, status=("OK" if ok else "FAIL"),
                             elapsed_ms=elapsed_ms, note=note)]

    rows: list[SourceCheck] = []
    for ds in datasets:
        key = str(ds.get("source_id", "")).rsplit(":", 1)[-1] or "dataset"
        ok = bool(ds.get("ok"))
        note = _summarise(ds) if ok else (ds.get("error") or "no error detail")
        rows.append(SourceCheck(
            name=f"oceancolour_{key}", ok=ok, status=("OK" if ok else "FAIL"),
            elapsed_ms=elapsed_ms, note=note,
        ))
    return rows


def run_checks(region: RegionConfig | None = None) -> list[SourceCheck]:
    """Thin loop: call every adapter's health probe (and, where one is configured, its
    currency check) and collect the results.

    All the actual network I/O happens here (inside the lazily-imported probe functions
    above via each adapter's own ``Source.get``/``health()``, plus the one extra
    ``nearest_pfz_line`` lookup ``_pfz_currency`` makes). Nothing in this function does
    its own HTTP — it only times and isolates each call.
    """
    region = region or load_region()
    results = [_check(spec, region) for spec in CHECKS]
    results.extend(_oceancolour_checks(region))
    return results


def format_report(results: list[SourceCheck]) -> tuple[str, int]:
    """Pure, network-free: render a summary table + verdict line, and the exit code.

    Backward-compatible with any duck-typed row that only carries the original four
    attributes (``name``/``ok``/``elapsed_ms``/``note``, no ``status``) — the display
    status for such a row is derived from ``ok`` exactly as it always was, via
    ``getattr(r, "status", None)``. ``ok_count`` counts rows whose *displayed* status is
    ``"OK"`` — not ``r.ok`` directly, because a STALE row still has ``ok=True`` (the
    probe itself succeeded) and counting it into "sources OK" would both overstate that
    line and make it disagree with the STALE count printed right next to it. For a
    caller with no STALE rows this is identical to counting ``r.ok`` (a duck-typed row
    with no ``status`` attribute derives ``"OK"``/``"FAIL"`` from ``ok`` one-to-one), so
    output for every pre-existing test is byte-identical to before this fix — see
    ``backend/tests/test_healthcheck.py``'s pre-existing tests, none of which needed to
    change.

    **Exit code:** non-zero if any row is FAIL *or* STALE. This script is read by a
    human once a morning, and the entire point of adding STALE is that "reachable" and
    "usable" stopped meaning the same thing for two sources that both still returned
    ``ok=True`` — a PFZ line frozen at 2021 and a chlorophyll grid centred on the
    Pacific. Letting STALE leave the exit code at 0 would just move the same silent-pass
    bug one layer up, from inside `health()` to inside this function. STALE is therefore
    exit-significant exactly like FAIL; the two remain visually distinct in the table and
    in the summary line so a human still knows which kind of "not OK" they are looking
    at, and only ``FAIL`` is the "totally unreachable" case CLAUDE.md's original "0 if
    every source is OK, 1 if any FAILED" line described — STALE is new, additive, and
    equally exit-significant.
    """
    name_w = max([len("source")] + [len(r.name) for r in results]) + 2
    status_w = 8
    elapsed_w = 12

    lines: list[str] = []
    header = f"{'source':<{name_w}}{'status':<{status_w}}{'elapsed_ms':>{elapsed_w}}  note"
    lines.append(header)
    lines.append("-" * len(header))

    statuses: list[str] = []
    for r in results:
        status = getattr(r, "status", None) or ("OK" if r.ok else "FAIL")
        statuses.append(status)
        lines.append(f"{r.name:<{name_w}}{status:<{status_w}}{r.elapsed_ms:>{elapsed_w}}  {r.note}")

    total = len(results)
    ok_count = statuses.count("OK")
    stale_count = statuses.count("STALE")
    fail_count = statuses.count("FAIL")

    lines.append("")
    summary_line = f"{ok_count}/{total} sources OK"
    if stale_count:
        summary_line += f", {stale_count} STALE"
    if fail_count:
        summary_line += f", {fail_count} FAIL"
    lines.append(summary_line)

    # ok_count == total already implies stale_count == fail_count == 0 (the three counts
    # partition `results`), so this is exactly "no FAIL and no STALE" — see the exit-code
    # rationale above.
    exit_code = 0 if ok_count == total else 1
    return "\n".join(lines), exit_code


def main(argv: list[str] | None = None) -> int:
    region = load_region()
    print(
        f"FORESHORE source healthcheck — region={region.region_id} "
        f"({region.display_name_en}); FORESHORE_MODE={mode()}"
    )
    print()

    results = run_checks(region=region)
    text, exit_code = format_report(results)
    print(text)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
