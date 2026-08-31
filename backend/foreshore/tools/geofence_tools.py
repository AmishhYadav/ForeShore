"""Tools 9 and 10 — geofence proximity and router exclusion zones.

``check_geofences`` is the single most safety-relevant tool in the registry: it is the
proximity computation that eventually drives the push-path alert loop and the boat UI's
client-side warning (the same computation the geofence engine documents as running with
no network, offshore, against cached polygons). It must never quietly answer "clear"
when it actually could not check — a "no fences nearby" result and a "cannot check"
result must be structurally distinguishable to the caller.

``get_exclusion_zones`` assembles everything the A* router must treat as impassable:
dynamic cyclone hazard polygons from GDACS plus the hard legal-boundary and MPA layers
from the vector store.

The :class:`~foreshore.geofence.engine.GeofenceEngine` and
:class:`~foreshore.store.vectors.VectorStore` are pure, local, dependency-free modules
(no network I/O) — they are constructed once and cached at module level per the
project's push-loop performance constraint, rather than lazily-imported-per-call the way
network adapters are.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from ..config import RegionConfig, load_region
from ..geofence.classes import ALERT_RANK, describe_classes, region_layers
from ..geofence.engine import GeofenceEngine
from ..models import (
    GeofenceClass,
    GeofenceProximity,
    Observation,
    Provenance,
    ToolResult,
    utcnow,
)
from ..store.vectors import VectorStore
from .registry import latlon_schema, registry

#: Layers that are structurally dynamic-only (cyclone/hazard cones held in memory, or a
#: user-drawn boundary that may legitimately never have been created) and therefore must
#: never be treated as "the static fetch job has not run yet" when absent from the
#: vector store.
_DYNAMIC_OR_OPTIONAL_LAYERS: frozenset[str] = frozenset({"hazard_exclusion", "user_defined"})

#: Classes whose absence changes the legal answer rather than an advisory one. A missing
#: coral layer degrades the advice; a missing IMBL layer means the boundary itself is
#: unverified, which the caller must be able to distinguish.
HARD_CLASSES: frozenset[str] = frozenset({"IMBL_HISTORIC_WATERS", "IMBL_MARITIME_BOUNDARY"})

_engine_instance: GeofenceEngine | None = None
_store_instance: VectorStore | None = None


def _store() -> VectorStore:
    """Module-level cached :class:`VectorStore` — built once, reused by every call."""
    global _store_instance
    if _store_instance is None:
        _store_instance = VectorStore()
    return _store_instance


def _engine() -> GeofenceEngine:
    """Module-level cached :class:`GeofenceEngine` — expensive to build per call, and
    the push loop calls ``check_geofences`` for every tracked vessel every few seconds."""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = GeofenceEngine(store=_store())
    return _engine_instance


def _required_static_layers(
    region: RegionConfig, classes: Sequence[str] | None
) -> dict[str, GeofenceClass]:
    """Static layer ids this check needs to be trustworthy, given an optional class
    filter. Excludes layers that are structurally dynamic or optional (see
    :data:`_DYNAMIC_OR_OPTIONAL_LAYERS`) — their absence does not mean the static fetch
    job has not run."""
    layers = {
        lid: gc for lid, gc in region_layers(region).items()
        if lid not in _DYNAMIC_OR_OPTIONAL_LAYERS
    }
    if classes:
        wanted = set(classes)
        layers = {lid: gc for lid, gc in layers.items() if gc in wanted}
    return layers


def _observation_for_proximity(prox: GeofenceProximity, lat: float, lon: float) -> Observation:
    return Observation(
        variable="geofence_distance",
        value=round(prox.distance_nm, 3),
        unit="nm",
        lat=lat,
        lon=lon,
        valid_time=utcnow(),
        provenance=prox.provenance,
        qualifiers={
            "geofence_class": prox.geofence_class,
            "name": prox.name,
            "severity": prox.severity,
            "level": prox.level,
            "bearing_deg": prox.bearing_deg,
            "inside": prox.inside,
            "eta_seconds": prox.eta_seconds,
        },
    )


@registry.tool(
    name="check_geofences",
    number=9,
    description=(
        "Distance, bearing, alert level and closing ETA from a vessel position to every "
        "geofence in range: IMBL historic-waters and maritime-boundary lines, marine "
        "protected areas, ecologically sensitive habitats, user-defined operational "
        "boundaries, and any active dynamic hazard exclusions. The single most "
        "safety-relevant tool in the system — a 'no fences nearby' answer and a 'cannot "
        "check' answer are never the same response."
    ),
    schema=latlon_schema(
        heading_deg={
            "type": "number",
            "description": "Vessel heading, degrees true (0-360). Optional; enables closing-ETA.",
        },
        speed_kn={
            "type": "number",
            "description": "Vessel speed over ground, knots. Optional; enables closing-ETA.",
        },
        classes={
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "IMBL_HISTORIC_WATERS",
                    "IMBL_MARITIME_BOUNDARY",
                    "MPA",
                    "ECO_SENSITIVE",
                    "USER_DEFINED",
                    "HAZARD_EXCLUSION",
                ],
            },
            "description": "Restrict the check to these geofence classes. Omit to check all.",
        },
    ),
    specialists=("GeospatialReasoning", "VisualizationAgent"),
    reads_sources=(),
    cost="fast",
)
def check_geofences(
    lat: float,
    lon: float,
    heading_deg: float | None = None,
    speed_kn: float | None = None,
    classes: Sequence[str] | None = None,
) -> ToolResult:
    """Wrap :meth:`GeofenceEngine.check`, guarding the "layers not fetched yet" case.

    Returns ``ok=True, partial=True, missing=["static_geofence_layers"]`` — never a
    silent empty "clear" — when the static layers this check needs have not been
    fetched by ``scripts/fetch_static.py``.
    """
    region = load_region()
    wanted_classes: list[GeofenceClass] | None = list(classes) if classes else None  # type: ignore[list-item]

    try:
        required = _required_static_layers(region, wanted_classes)
        present = set(_store().layers())
    except Exception as exc:  # noqa: BLE001 — the store itself must not crash the tool
        return ToolResult(
            tool="check_geofences",
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            summary=f"Failed to inspect the static geofence layers: {exc}",
            missing=["static_geofence_layers"],
        )

    missing_layers = sorted(set(required) - present)
    checkable = {lid: gc for lid, gc in required.items() if lid in present}
    # Which classes lost a layer, and which of those are legal-hard rather than advisory.
    unchecked_classes = sorted({required[lid] for lid in missing_layers})
    unchecked_hard = [c for c in unchecked_classes if c in HARD_CLASSES]

    if not checkable:
        # Nothing at all to measure against: the only honest answer is that the check
        # could not run. "No fences nearby" and "cannot check" are different responses.
        return ToolResult(
            tool="check_geofences",
            ok=True,
            partial=True,
            missing=["static_geofence_layers"],
            summary=(
                "Geofence proximity cannot be computed yet: the static geofence layers "
                "have not been fetched (run scripts/fetch_static.py), so no boundary can "
                "be confirmed either clear or crossed. This is not a 'no fences nearby' "
                "answer."
            ),
            payload={
                "proximities": [],
                "messages": {lang: [] for lang in region.languages},
                "legend": describe_classes(region.primary_language),
                "worst_level": None,
                "classes_present": [],
                "missing_layers": missing_layers,
                "available_layers": sorted(present),
            },
        )

    try:
        engine = _engine()
        results = engine.check(
            lat, lon, heading_deg=heading_deg, speed_kn=speed_kn, classes=wanted_classes
        )
    except Exception as exc:  # noqa: BLE001 — a computation bug must abstain, not crash
        return ToolResult(
            tool="check_geofences",
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            summary=f"Geofence proximity computation failed: {exc}",
            missing=["geofence_check"],
        )

    observations = [_observation_for_proximity(p, lat, lon) for p in results]
    messages = {lang: [engine.message(p, lang) for p in results] for lang in region.languages}
    worst_level = max((p.level for p in results), key=lambda lvl: ALERT_RANK[lvl], default="INFO")
    classes_present = sorted({p.geofence_class for p in results})

    if results:
        summary = "; ".join(
            f"{p.geofence_class} '{p.name}' {p.distance_nm:.2f} nm ({p.level})" for p in results
        )
    else:
        summary = (
            "No geofences (IMBL boundaries, MPA, eco-sensitive habitats, user-defined "
            "or hazard-exclusion zones) are within warning range of this position."
        )

    payload = {
        "proximities": [p.to_dict() for p in results],
        "messages": messages,
        "legend": describe_classes(region.primary_language),
        "worst_level": worst_level,
        "classes_present": classes_present,
        "missing_layers": missing_layers,
        "unchecked_classes": unchecked_classes,
        "available_layers": sorted(present),
    }
    if missing_layers:
        # A partial check is still worth far more than no check: the 1974 line is the
        # fence that gets fishermen arrested, and it must not be masked by an advisory
        # habitat layer that a flaky upstream refused to serve. Say exactly which
        # classes went unchecked, and say it louder when a legal boundary is one of them.
        note = (
            "Not all geofence classes could be checked: "
            + ", ".join(unchecked_classes)
            + f" (missing layers: {', '.join(missing_layers)}). "
            + (
                "A LEGAL boundary is among them, so this position cannot be declared "
                "clear of the maritime boundary."
                if unchecked_hard
                else "The classes checked below are complete; the missing ones are advisory."
            )
        )
        summary = f"{summary} {note}"
        payload["unchecked_note"] = note

    return ToolResult(
        tool="check_geofences",
        ok=True,
        partial=bool(missing_layers),
        missing=(["static_geofence_layers"] if unchecked_hard else []),
        observations=observations,
        payload=payload,
        summary=summary,
    )


# --------------------------------------------------------------------------------------
# tool 10 — get_exclusion_zones
# --------------------------------------------------------------------------------------


def _static_layer_provenance(store: VectorStore, layer_id: str) -> Provenance:
    """Best-effort provenance for a static vector layer, mirroring the same authority
    inference :class:`GeofenceEngine` uses internally, without reaching into its
    private methods."""
    meta: dict[str, Any] = {}
    try:
        meta = store.layer_meta(layer_id) or {}
    except Exception:  # noqa: BLE001 — a missing/corrupt sidecar must not crash
        meta = {}
    acquired_raw = meta.get("acquired_at")
    acquired_at = (
        datetime.fromisoformat(acquired_raw) if isinstance(acquired_raw, str) else utcnow()
    )
    authority: Any = "VLIZ" if layer_id.startswith("imbl") else "INCOIS"
    if layer_id.startswith("mpa"):
        authority = "derived"
    return Provenance(
        source_id=meta.get("source_id", layer_id),
        source_name=f"FORESHORE geofence layer '{layer_id}'",
        authority=authority,
        url=f"local://static/{layer_id}.geojson",
        acquired_at=acquired_at,
        issued_at=acquired_at,
    )


@registry.tool(
    name="get_exclusion_zones",
    number=10,
    description=(
        "Every zone the router must treat as impassable, as tagged GeoJSON features: "
        "active GDACS cyclone forecast cones and red/orange wind-radii polygons, the "
        "hard IMBL historic-waters and maritime-boundary layers, and any marine "
        "protected area. Zero active cyclone exclusions is a common, valid outcome and "
        "is stated positively, not as a failure."
    ),
    schema={
        "type": "object",
        "properties": {
            "when": {
                "type": "string",
                "description": (
                    "Optional ISO 8601 timestamp for a hypothetical check. Upstream "
                    "sources (GDACS, static layers) only expose the current state, so a "
                    "non-current value is recorded but not filtered on."
                ),
            },
            "bbox": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
                "description": (
                    "Optional [minlon, minlat, maxlon, maxlat] in EPSG:4326. Defaults to "
                    "the active region's bbox."
                ),
            },
        },
        "required": [],
    },
    specialists=("GeospatialReasoning", "RoutingAgent", "VisualizationAgent"),
    reads_sources=("gdacs_tc",),
    cost="slow",
)
def get_exclusion_zones(
    when: str | None = None, bbox: Sequence[float] | None = None
) -> ToolResult:
    """Assemble router-blocking hazard/legal-boundary/MPA features from every source.

    A single failing source (GDACS unreachable, a static layer not yet fetched) is
    recorded in ``payload["sources_failed"]`` rather than failing the whole tool — the
    router still needs whatever exclusions it *can* get.
    """
    region = load_region()
    bbox_final = tuple(bbox) if bbox else region.bbox

    features: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    sources_checked: list[str] = []
    sources_failed: list[str] = []
    observations: list[Observation] = []
    notes: list[str] = []

    if when:
        notes.append(
            f"'when'={when!r} was requested but upstream sources only expose current "
            "state; results reflect now, not that timestamp."
        )

    # -- GDACS cyclone hazard polygons ---------------------------------------------
    sources_checked.append("gdacs_tc")
    try:
        from ..sources.gdacs import GDACS_EVENTLIST, GDACSCyclones

        gdacs = GDACSCyclones(region=region)
        polygons, gdacs_obs = gdacs.exclusion_polygons()
        for feat in polygons:
            props = dict(feat.get("properties") or {})
            props.setdefault("geofence_class", "HAZARD_EXCLUSION")
            hazard_class = props.get("hazard_class", "cyclone_hazard")
            counts[hazard_class] = counts.get(hazard_class, 0) + 1
            features.append({**feat, "properties": props})

        cyclone_prov = gdacs_obs[0].provenance if gdacs_obs else Provenance(
            source_id="gdacs_tc",
            source_name="GDACS Tropical Cyclone alerts (JRC / European Commission)",
            authority="JRC/GDACS",
            url=GDACS_EVENTLIST,
            acquired_at=utcnow(),
            issued_at=utcnow(),
        )
        observations.append(
            Observation(
                variable="exclusion_zone_count",
                value=len(polygons),
                unit="count",
                lat=region.centre[0],
                lon=region.centre[1],
                valid_time=cyclone_prov.acquired_at,
                provenance=cyclone_prov,
                qualifiers={"hazard_class": "cyclone_exclusion"},
            )
        )
        if not polygons:
            notes.append(
                "0 active GDACS cyclone exclusion polygons near this region — no current "
                "tropical cyclone threatens this coast, a common valid outcome."
            )
    except Exception as exc:  # noqa: BLE001 — one source failing must not sink the tool
        sources_failed.append(f"gdacs_tc: {type(exc).__name__}: {exc}")

    # -- static hard-boundary + MPA layers ------------------------------------------
    store = _store()
    static_layers: list[tuple[str, GeofenceClass]] = [
        ("imbl_historic_waters", "IMBL_HISTORIC_WATERS"),
        ("imbl_maritime_boundary", "IMBL_MARITIME_BOUNDARY"),
    ]
    for layer_id, gclass in region_layers(region).items():
        if gclass == "MPA":
            static_layers.append((layer_id, gclass))

    try:
        available = set(store.layers())
    except Exception as exc:  # noqa: BLE001
        available = set()
        sources_failed.append(f"vector_store: {type(exc).__name__}: {exc}")

    for layer_id, gclass in static_layers:
        sources_checked.append(layer_id)
        if layer_id not in available:
            sources_failed.append(f"{layer_id}: static layer not fetched (run scripts/fetch_static.py)")
            counts.setdefault(layer_id, 0)
            continue
        try:
            feats = store.intersecting_bbox(layer_id, bbox_final)
            n = 0
            for f in feats:
                features.append(
                    {
                        "type": "Feature",
                        "geometry": f.geometry,
                        "properties": {
                            **f.properties,
                            "hazard_class": layer_id,
                            "geofence_class": gclass,
                        },
                    }
                )
                n += 1
            counts[layer_id] = n
            prov = _static_layer_provenance(store, layer_id)
            observations.append(
                Observation(
                    variable="exclusion_zone_count",
                    value=n,
                    unit="count",
                    lat=region.centre[0],
                    lon=region.centre[1],
                    valid_time=prov.acquired_at,
                    provenance=prov,
                    qualifiers={"hazard_class": layer_id, "geofence_class": gclass},
                )
            )
        except Exception as exc:  # noqa: BLE001
            sources_failed.append(f"{layer_id}: {type(exc).__name__}: {exc}")

    summary_bits = [f"{k}: {v}" for k, v in sorted(counts.items())]
    summary = (
        ("Exclusion zones — " + "; ".join(summary_bits) + ".") if summary_bits else
        "No exclusion-zone features found from any source."
    )
    if notes:
        summary = summary + " " + " ".join(notes)
    if sources_failed:
        summary = summary + f" ({len(sources_failed)} source(s) unavailable, see sources_failed.)"

    payload = {
        "features": features,
        "counts": counts,
        "sources_checked": sources_checked,
        "sources_failed": sources_failed,
        "notes": notes,
    }
    return ToolResult(
        tool="get_exclusion_zones",
        ok=True,
        partial=bool(sources_failed),
        missing=sources_failed,
        observations=observations,
        payload=payload,
        summary=summary,
    )


__all__ = ["check_geofences", "get_exclusion_zones"]
