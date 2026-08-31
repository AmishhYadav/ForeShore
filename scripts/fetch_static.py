"""One-shot pull of every FORESHORE layer that does not change day to day.

Run once, commit the result, and the geofence engine, the router and both UIs work with
the network switched off. Every layer below is written through
:meth:`foreshore.store.vectors.VectorStore.write_layer` so it lands at
``data/static/<layer_id>.geojson`` (+ a ``.meta.json`` provenance sidecar) exactly the way
the rest of the system expects to find it.

What each layer is for
-----------------------
``imbl_historic_waters`` / ``imbl_maritime_boundary``
    The India-Sri Lanka International Maritime Boundary Line, split into its two legally
    distinct regimes (1974 historic-waters vs. 1976 maritime-boundary treaty segments) per
    ``backend/foreshore/geofence/classes.py``. This is the hardest, least forgiving fence
    in the system — crossing it risks arrest — so it is fetched from Marine Regions/VLIZ,
    never hand-digitised.
``eco_coral`` / ``eco_seagrass`` / ``eco_mangrove``
    INCOIS MHW ecologically-sensitive habitat polygons. Advisory severity: anchoring/
    trawling over them is discouraged, not illegal. ``eco_mangrove`` is documented by
    INCOIS's own adapter as intermittently flaky (502/503), so it gets extra retries and,
    on exhaustion, a loud warning and a SKIPPED row rather than a silent empty layer.
``landing_centres``
    Every named INCOIS landing centre in (and, if none fall inside the bbox, nationally
    around) the region. This is what a ``DO_NOT_ADVISE`` verdict hands a fisherman off to
    — it is a REQUIRED layer.
``pfz_sectors``
    The INCOIS PFZ sector polygon(s) for this region (``SEC006`` = South Tamil Nadu for
    the demo region), for sanity-checking which sector a position falls in.
``bathymetry``
    INCOIS depth-contour lines, for the router's shallow-water cost term.
``mpa_<id>``
    One polygon layer per MPA the region config declares under ``geofences.mpa``. If a
    hand-supplied raw file already exists at the entry's configured ``source_file`` it is
    ingested as-is (and left untouched on disk). Otherwise this script builds a clearly
    labelled *approximate* polygon (see :func:`build_approx_mpa_feature`) — it is never
    presented as the authoritative park boundary.
``coastline``
    A land mask for the router's "is this cell water" check, clipped to the region bbox
    padded by 0.5 degrees. Sourced from Natural Earth's 10 m land polygons (a keyless raw
    GitHub file) — see :func:`fetch_coastline` for the fallback order.

Every layer is independent: one failing must never stop the others, and only
``imbl_historic_waters``, ``imbl_maritime_boundary`` and ``landing_centres`` are required
for a clean (exit 0) run. Everything else is best-effort.

CLI
---
    python scripts/fetch_static.py [--region palk_bay_gom] [--layers a,b,c] [--force] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx
import orjson
from shapely.geometry import LineString, box as shapely_box, mapping, shape as shapely_shape
from shapely.ops import unary_union

# Make `import foreshore` work whether or not the package is installed editable into the
# active venv (it is, today — but a script that only works by accident of installation
# order is a landmine for whoever runs this next).
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from foreshore.config import RegionConfig, STATIC_DIR, load_region  # noqa: E402
from foreshore.models import UTC, utcnow  # noqa: E402
from foreshore.sources.base import BROWSER_UA  # noqa: E402
from foreshore.store.vectors import VectorStore  # noqa: E402

REQUIRED_LAYERS: tuple[str, ...] = (
    "imbl_historic_waters",
    "imbl_maritime_boundary",
    "landing_centres",
)

NATURAL_EARTH_10M_LAND = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
    "ne_10m_land.geojson"
)

#: Documented cartographic hint, not a hardcoded region boundary: for the one MPA id the
#: brief names explicitly, the real 21-island chain runs between these two named anchor
#: ports (looked up from region config, never as raw coordinates here). Any MPA id not
#: listed falls back to the fully generic "line through every anchor port" construction
#: below, so a region swap (e.g. Gulf of Kutch) never crashes for lack of an entry here.
_MPA_CHAIN_ENDPOINTS: dict[str, tuple[str, str]] = {
    "gom_marine_national_park": ("Rameswaram", "Tuticorin"),
}

#: Half-width of the documented-approximate MPA buffer, in degrees (~5 km).
_MPA_APPROX_BUFFER_DEG = 0.045


# ------------------------------------------------------------------------------------
# bookkeeping
# ------------------------------------------------------------------------------------


@dataclass
class LayerRun:
    layer_id: str
    required: bool
    status: str = "PENDING"  # OK | SKIPPED | FAILED | WOULD-FETCH | DRY-SKIP
    feature_count: int = 0
    source: str = ""
    bytes_on_disk: int = 0
    note: str = ""


def _geojson_path(layer_id: str) -> Path:
    return STATIC_DIR / f"{layer_id}.geojson"


def _existing_row(layer_id: str, required: bool, store: VectorStore, status: str) -> LayerRun:
    meta = store.layer_meta(layer_id)
    path = _geojson_path(layer_id)
    return LayerRun(
        layer_id=layer_id,
        required=required,
        status=status,
        feature_count=int(meta.get("count") or 0),
        source=str(meta.get("source_id") or "(existing file, no source_id recorded)"),
        bytes_on_disk=path.stat().st_size if path.exists() else 0,
        note="already present on disk" if status == "SKIPPED" else "would be skipped (exists)",
    )


def run_layer(
    layer_id: str,
    required: bool,
    build: Callable[[], tuple[int, str, str]],
    *,
    store: VectorStore,
    force: bool,
    dry_run: bool,
) -> LayerRun:
    """Execute one layer build, translating any exception into a FAILED row.

    A failure here is deliberately swallowed at this level (never re-raised) — the CLI
    contract is that one bad layer must never take the others down with it.
    """
    exists = _geojson_path(layer_id).exists()

    if dry_run:
        if exists and not force:
            return _existing_row(layer_id, required, store, "DRY-SKIP")
        return LayerRun(layer_id=layer_id, required=required, status="WOULD-FETCH",
                         note="no network call made (--dry-run)")

    if exists and not force:
        return _existing_row(layer_id, required, store, "SKIPPED")

    try:
        count, source_label, note = build()
        path = _geojson_path(layer_id)
        size = path.stat().st_size if path.exists() else 0
        return LayerRun(
            layer_id=layer_id, required=required, status="OK",
            feature_count=count, source=source_label, bytes_on_disk=size, note=note,
        )
    except Exception as exc:  # noqa: BLE001 - a layer failing must never abort the others
        msg = f"{type(exc).__name__}: {exc}"
        print(f"WARNING: layer '{layer_id}' failed: {msg}", file=sys.stderr)
        return LayerRun(layer_id=layer_id, required=required, status="FAILED", note=msg)


# ------------------------------------------------------------------------------------
# IMBL — one shared fetch, two layers
# ------------------------------------------------------------------------------------

_imbl_cache: dict[str, Any] = {}


def _imbl_segments(region: RegionConfig) -> tuple[Any, list[Any], Any]:
    """Fetch Marine Regions/VLIZ treaty segments once and cache the successful result.

    Imported lazily: ``marine_regions.py`` may still be in flight from another agent, and
    an import error here must degrade to "this layer failed", never crash the script.
    """
    if "result" in _imbl_cache:
        return _imbl_cache["result"]
    from foreshore.sources.marine_regions import MarineRegionsIMBL  # noqa: PLC0415

    src = MarineRegionsIMBL(region=region)
    segs, raw = src.segments()
    result = (src, segs, raw)
    _imbl_cache["result"] = result
    return result


def fetch_imbl_class(
    store: VectorStore, region: RegionConfig, geofence_class: str, layer_id: str
) -> tuple[int, str, str]:
    src, segs, raw = _imbl_segments(region)
    matching = [s for s in segs if s.geofence_class == geofence_class]
    features = [s.to_geojson_feature() for s in matching]
    count = store.write_layer(
        layer_id, features, source_id=src.source_id, acquired_at=raw.acquired_at,
        key_property="line_id",
    )
    line_ids = sorted(s.line_id for s in matching)
    note = f"{len(matching)}/{len(segs)} treaty segments matched; line_ids={line_ids}"
    return count, src.source_name, note


# ------------------------------------------------------------------------------------
# INCOIS WFS layers
# ------------------------------------------------------------------------------------


def fetch_eco_zone(
    store: VectorStore, region: RegionConfig, kind: str, layer_id: str, *, retries: int = 1
) -> tuple[int, str, str]:
    from foreshore.sources.incois_wfs import IncoisWFS  # noqa: PLC0415

    src = IncoisWFS(region=region)
    attempts = retries + 1
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            feats, raw = src.eco_zones(kind)
            count = store.write_layer(layer_id, feats, source_id=src.source_id, acquired_at=raw.acquired_at)
            note = f"{count} feature(s) (attempt {attempt + 1}/{attempts})"
            return count, src.source_name, note
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < attempts - 1:
                print(
                    f"WARNING: {layer_id} attempt {attempt + 1}/{attempts} failed "
                    f"({type(exc).__name__}: {exc}); retrying...",
                    file=sys.stderr,
                )
                time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"eco_zones({kind!r}) failed after {attempts} attempt(s): {last_exc}")


def fetch_landing_centres(store: VectorStore, region: RegionConfig) -> tuple[int, str, str]:
    from foreshore.sources.incois_wfs import IncoisWFS  # noqa: PLC0415

    src = IncoisWFS(region=region)
    feats, raw = src.landing_centres()
    count = store.write_layer(
        "landing_centres", feats, source_id=src.source_id, acquired_at=raw.acquired_at,
        key_property="LC_UNIQUE_",
    )
    return count, src.source_name, f"{count} landing centre(s) in/near region bbox"


def fetch_pfz_sectors(store: VectorStore, region: RegionConfig) -> tuple[int, str, str]:
    from foreshore.sources.incois_wfs import IncoisWFS  # noqa: PLC0415

    src = IncoisWFS(region=region)
    feats, raw = src.pfz_sectors()
    count = store.write_layer(
        "pfz_sectors", feats, source_id=src.source_id, acquired_at=raw.acquired_at,
        key_property="SEC_ID",
    )
    return count, src.source_name, f"{count} sector(s)"


def fetch_bathymetry(store: VectorStore, region: RegionConfig) -> tuple[int, str, str]:
    from foreshore.sources.incois_wfs import IncoisWFS  # noqa: PLC0415

    src = IncoisWFS(region=region)
    feats, raw = src.bathymetry()
    count = store.write_layer("bathymetry", feats, source_id=src.source_id, acquired_at=raw.acquired_at)
    return count, src.source_name, f"{count} depth-contour segment(s)"


# ------------------------------------------------------------------------------------
# MPA — manual override, else a documented approximation
# ------------------------------------------------------------------------------------


def build_approx_mpa_feature(region: RegionConfig, entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """A clearly-labelled approximate MPA polygon: a buffer around the line connecting
    named anchor ports, clipped to the region bbox.

    Never hardcodes a coordinate — the ports and bbox both come from ``load_region``. The
    only region-specific thing here is *which two named ports* bound a *known* island
    chain for one specific, brief-documented MPA id; every other MPA id (a region swap's
    own park) falls back to a line through every configured anchor port, which is honest
    about being a rough guess rather than a chain-following shape.
    """
    mpa_id = str(entry.get("id"))
    endpoint_names = _MPA_CHAIN_ENDPOINTS.get(mpa_id)
    ports = None
    method = "generic: line through every configured anchor port"
    if endpoint_names:
        found = [region.port(name) for name in endpoint_names]
        if all(found):
            ports = found
            method = f"documented: line between anchor ports {list(endpoint_names)}"
    if ports is None:
        ports = list(region.anchor_ports)

    minlon, minlat, maxlon, maxlat = region.bbox
    clip = shapely_box(minlon, minlat, maxlon, maxlat)

    if len(ports) >= 2:
        line = LineString([(p.lon, p.lat) for p in ports])
        poly = line.buffer(_MPA_APPROX_BUFFER_DEG)
    else:
        # Degenerate region config (a single anchor port): fall back to a small disc
        # around the region centre so the layer is at least non-empty.
        lat, lon = region.centre
        from shapely.geometry import Point  # noqa: PLC0415

        poly = Point(lon, lat).buffer(_MPA_APPROX_BUFFER_DEG * 2)
        method = "degenerate fallback: buffer around region centroid (fewer than 2 anchor ports)"

    poly = poly.intersection(clip)
    buffer_km = _MPA_APPROX_BUFFER_DEG * 111.0
    provenance_note = (
        f"No reliable keyless authoritative polygon source for "
        f"'{entry.get('name_en', mpa_id)}' was located within this script's scope. "
        f"THIS IS A DOCUMENTED APPROXIMATION, not the authoritative park boundary: "
        f"a ~{buffer_km:.1f} km-wide buffer ({method}), clipped to the region bounding "
        f"box. Built by scripts/fetch_static.py:build_approx_mpa_feature on "
        f"{utcnow().date().isoformat()}. Replace by dropping a real boundary file at "
        f"data/static/{entry.get('source_file')} and re-running with --force."
    )
    properties = {
        "id": mpa_id,
        "name_en": entry.get("name_en"),
        "name_local": entry.get("name_local"),
        "is_approximate": True,
        "provenance_note": provenance_note,
    }
    return {"type": "Feature", "geometry": mapping(poly), "properties": properties}, provenance_note


def fetch_mpa(store: VectorStore, region: RegionConfig, entry: dict[str, Any]) -> tuple[int, str, str]:
    mpa_id = str(entry.get("id"))
    layer_id = f"mpa_{mpa_id}"
    source_file = entry.get("source_file")
    manual_path = STATIC_DIR / source_file if source_file else None

    if manual_path is not None and manual_path.exists():
        raw_fc = orjson.loads(manual_path.read_bytes())
        feats = raw_fc.get("features") if isinstance(raw_fc, dict) else None
        if feats is None and isinstance(raw_fc, dict) and raw_fc.get("type") == "Feature":
            feats = [raw_fc]
        feats = feats or []
        for f in feats:
            props = dict(f.get("properties") or {})
            props.setdefault("id", mpa_id)
            props.setdefault("name_en", entry.get("name_en"))
            props.setdefault("name_local", entry.get("name_local"))
            props.setdefault("is_approximate", False)
            f["properties"] = props
        count = store.write_layer(
            layer_id, feats, source_id=f"manual:{source_file}", acquired_at=utcnow(),
        )
        return (
            count,
            f"hand-supplied file data/static/{source_file}",
            f"ingested {count} feature(s) from the pre-existing raw file; left it untouched",
        )

    feature, note = build_approx_mpa_feature(region, entry)
    count = store.write_layer(
        layer_id, [feature], source_id="derived:approx_mpa_polygon", acquired_at=utcnow(),
    )
    return count, "FORESHORE-derived documented approximation", note


# ------------------------------------------------------------------------------------
# Coastline / land mask
# ------------------------------------------------------------------------------------


def _clip_land_features(
    features: list[dict[str, Any]], clip: Any
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in features:
        geom = f.get("geometry")
        if not geom:
            continue
        try:
            shp = shapely_shape(geom)
        except Exception:
            continue
        if not shp.is_valid:
            shp = shp.buffer(0)
        if not shp.intersects(clip):
            continue
        clipped = shp.intersection(clip)
        if clipped.is_empty:
            continue
        if clipped.geom_type == "GeometryCollection":
            polys = [g for g in clipped.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
            if not polys:
                continue
            clipped = unary_union(polys)
            if clipped.is_empty:
                continue
        props = dict(f.get("properties") or {})
        props["geometry_kind"] = "polygon"
        out.append({"type": "Feature", "geometry": mapping(clipped), "properties": props})
    return out


def fetch_coastline(store: VectorStore, region: RegionConfig) -> tuple[int, str, str]:
    """Land mask for the router, tried in the order the brief specifies.

    (a) Natural Earth 10 m land polygons, a keyless raw-GitHub file — the primary path,
        and the only one actually exercised unless it fails: verified reachable and to
        yield the mainland-India, Sri Lanka and Palk-Strait-island polygons needed here.
    (b) An INCOIS/Bhuvan coastline WFS layer — skipped deliberately. Every keyless INCOIS
        GeoServer workspace and type name this project has verified live (see
        ``backend/foreshore/sources/incois_wfs.py`` and ``CLAUDE.md``) is enumerated
        there; none of them is a coastline/land layer, so guessing an unverified
        ``typeName`` would only add noise, not a real fallback.
    (c) OSM Overpass ``natural=coastline`` for the region bbox — attempted as a genuine
        fallback if (a) fails. (Probed live from this environment on 2026-08-31: every
        public Overpass mirror returned HTTP 406 to a bare GetCapabilities-equivalent
        query, independent of query content — recorded here so nobody re-probes it.)
    """
    minlon, minlat, maxlon, maxlat = region.bbox
    pad = 0.5
    clip = shapely_box(minlon - pad, minlat - pad, maxlon + pad, maxlat + pad)
    errors: list[str] = []

    try:
        resp = httpx.get(
            NATURAL_EARTH_10M_LAND, headers={"User-Agent": BROWSER_UA}, timeout=60.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
        fc = orjson.loads(resp.content)
        feats = _clip_land_features(fc.get("features", []), clip)
        if feats:
            count = store.write_layer(
                "coastline", feats, source_id="natural_earth_10m_land", acquired_at=utcnow(),
            )
            return (
                count,
                "Natural Earth 10m land (raw GitHub)",
                f"{count} land polygon(s), geometry_kind=polygon, clipped to bbox+{pad} deg",
            )
        errors.append("Natural Earth 10m land: 0 features intersected the padded bbox")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Natural Earth 10m land: {type(exc).__name__}: {exc}")

    try:
        overpass_query = (
            "[out:json][timeout:25];"
            f"way[\"natural\"=\"coastline\"]({minlat - pad},{minlon - pad},{maxlat + pad},{maxlon + pad});"
            "out geom;"
        )
        resp = httpx.get(
            "https://overpass-api.de/api/interpreter",
            params={"data": overpass_query},
            headers={"User-Agent": BROWSER_UA},
            timeout=40.0,
        )
        resp.raise_for_status()
        payload = resp.json()
        line_feats = []
        for el in payload.get("elements", []):
            geom = el.get("geometry")
            if not geom:
                continue
            coords = [[pt["lon"], pt["lat"]] for pt in geom]
            if len(coords) < 2:
                continue
            line_feats.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": coords},
                    "properties": {"geometry_kind": "line", "osm_id": el.get("id")},
                }
            )
        if line_feats:
            count = store.write_layer(
                "coastline", line_feats, source_id="osm_overpass_coastline", acquired_at=utcnow(),
            )
            return (
                count,
                "OSM Overpass natural=coastline",
                f"{count} coastline LineString(s), geometry_kind=line (fallback c)",
            )
        errors.append("OSM Overpass: 0 coastline ways returned")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"OSM Overpass: {type(exc).__name__}: {exc}")

    raise RuntimeError("; ".join(errors))


# ------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------


def _layer_plan(region: RegionConfig, store: VectorStore) -> list[tuple[str, bool, Callable[[], tuple[int, str, str]]]]:
    plan: list[tuple[str, bool, Callable[[], tuple[int, str, str]]]] = [
        (
            "imbl_historic_waters", True,
            lambda: fetch_imbl_class(store, region, "IMBL_HISTORIC_WATERS", "imbl_historic_waters"),
        ),
        (
            "imbl_maritime_boundary", True,
            lambda: fetch_imbl_class(store, region, "IMBL_MARITIME_BOUNDARY", "imbl_maritime_boundary"),
        ),
        ("eco_coral", False, lambda: fetch_eco_zone(store, region, "coral", "eco_coral")),
        ("eco_seagrass", False, lambda: fetch_eco_zone(store, region, "seagrass", "eco_seagrass")),
        ("eco_mangrove", False, lambda: fetch_eco_zone(store, region, "mangrove", "eco_mangrove", retries=3)),
        ("landing_centres", True, lambda: fetch_landing_centres(store, region)),
        ("pfz_sectors", False, lambda: fetch_pfz_sectors(store, region)),
        ("bathymetry", False, lambda: fetch_bathymetry(store, region)),
    ]
    for entry in (region.geofences or {}).get("mpa", []) or []:
        mpa_id = str(entry.get("id"))
        plan.append((f"mpa_{mpa_id}", False, lambda e=entry: fetch_mpa(store, region, e)))
    plan.append(("coastline", False, lambda: fetch_coastline(store, region)))
    return plan


def _select(
    plan: list[tuple[str, bool, Callable[[], tuple[int, str, str]]]], wanted: list[str] | None
) -> list[tuple[str, bool, Callable[[], tuple[int, str, str]]]]:
    if not wanted:
        return plan
    wanted_set = set(wanted)
    out = []
    for layer_id, required, fn in plan:
        if layer_id in wanted_set:
            out.append((layer_id, required, fn))
        elif "mpa" in wanted_set and layer_id.startswith("mpa_"):
            out.append((layer_id, required, fn))
    return out


def _print_table(rows: list[LayerRun]) -> None:
    headers = ("layer_id", "features", "status", "bytes", "source")
    widths = [24, 9, 11, 10, 42]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        cells = [
            r.layer_id + (" *" if r.required else ""),
            str(r.feature_count),
            r.status,
            str(r.bytes_on_disk),
            r.source[:40],
        ]
        print("  ".join(c.ljust(w) for c, w in zip(cells, widths)))
        if r.note:
            print(f"    -> {r.note}")
    print("-" * len(line))
    print("* = required for exit code 0")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-shot pull of FORESHORE's static layers.")
    parser.add_argument("--region", default=None, help="region id (default: $FORESHORE_REGION or palk_bay_gom)")
    parser.add_argument("--layers", default=None, help="comma-separated subset of layer ids to run")
    parser.add_argument("--force", action="store_true", help="re-fetch even if the layer file already exists")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; make no network calls or writes")
    args = parser.parse_args(argv)

    region = load_region(args.region)
    store = VectorStore()

    print(f"FORESHORE static fetch — region={region.region_id} ({region.display_name_en})")
    print(f"static dir: {STATIC_DIR}")
    if args.dry_run:
        print("(dry run — no network calls, no writes)")
    print()

    wanted = [s.strip() for s in args.layers.split(",")] if args.layers else None
    plan = _select(_layer_plan(region, store), wanted)

    rows: list[LayerRun] = []
    for layer_id, required, build in plan:
        row = run_layer(layer_id, required, build, store=store, force=args.force, dry_run=args.dry_run)
        rows.append(row)

    print()
    _print_table(rows)

    if args.dry_run:
        return 0

    required_rows = [r for r in rows if r.required]
    missing_required = [r.layer_id for r in required_rows if r.status not in ("OK", "SKIPPED")]
    if missing_required:
        print(f"\nFAILED required layer(s): {missing_required}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
