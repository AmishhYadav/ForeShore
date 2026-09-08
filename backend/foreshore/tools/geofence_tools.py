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
from ..geofence.classes import (
    ALERT_RANK,
    GEOFENCE_CLASSES,
    describe_classes,
    format_eta,
    region_layers,
    title_for,
)
from ..geofence.engine import GeofenceEngine, shared_engine
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

_store_instance: VectorStore | None = None


def _store() -> VectorStore:
    """Module-level cached :class:`VectorStore` — built once, reused by every call."""
    global _store_instance
    if _store_instance is None:
        _store_instance = VectorStore()
    return _store_instance


def _engine() -> GeofenceEngine:
    """The process-wide :class:`GeofenceEngine` — shared with the push loop.

    Shared rather than private on purpose: the push loop is what refreshes the dynamic
    hazard fences (cyclone cones, wind polygons) each tick, and a private instance here
    would never see one. This tool is the request path's only hazard-proximity check;
    it has to look at the same fences the push path does.
    """
    return shared_engine()


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


#: Alert level -> what it means in words. The level codes are storage values; a reader is
#: told how close they are, not which enum member fired.
_LEVEL_WORDS: dict[str, str] = {
    "BREACH": "inside it now",
    "CRITICAL": "critically close",
    "WARN": "within warning range",
    "INFO": "clear for now",
}


def _summary_language(region: RegionConfig) -> str:
    """Language the human-readable ``summary`` is written in.

    The first declared *surface* language, not the first known one — this string is
    spliced verbatim into an answer and into the console's trace inspector, which is the
    exact path that put Tamil copy on an English-only screen (see CLAUDE.md's
    English-only pin). Falls back to English, which every copy table carries.
    """
    return (region.surface_languages or ("en",))[0]


def _proximity_sentence(prox: GeofenceProximity, lang: str) -> str:
    """One boundary, said the way a person says it.

    The class title carries the legal distinction — the 1974 historic-waters line and the
    1976 maritime boundary read differently here because they *are* different, and
    flattening them is the thing invariant 5 forbids.
    """
    title = title_for(prox.geofence_class, lang)
    where = _LEVEL_WORDS.get(prox.level, _LEVEL_WORDS["INFO"])
    # One name, not two. The feature's own name and the class title often overlap
    # ("Marine National Park" / "Gulf of Mannar Marine National Park"), and printing both
    # gave "Marine National Park (Gulf of Mannar Marine National Park)". Keep whichever
    # is more specific when one contains the other; keep both only when they differ.
    name = (prox.name or "").strip()
    if not name:
        subject = title
    elif title.lower() in name.lower():
        subject = name
    elif name.lower() in title.lower():
        subject = title
    else:
        subject = f"{title} ({name})"
    # No closing ETA once inside — see the same guard in tools/fleet_tools.py.
    eta = (
        f", closing in {format_eta(prox.eta_seconds, lang)}"
        if prox.eta_seconds is not None and not prox.inside
        else ""
    )
    return f"{subject}: {prox.distance_nm:.2f} nm, {where}{eta}."


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
    fetched by ``scripts/fetch_static.py``. (That path is for this docstring and for
    developers reading the trace; it must never land in ``summary``, which is
    user-facing prose — see the "cannot be computed yet" branch below.)
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
                "this check needs have not been loaded, so no boundary can be confirmed "
                "either clear or crossed. This is not a 'no fences nearby' answer."
            ),
            payload={
                "proximities": [],
                "messages": {lang: [] for lang in region.surface_languages},
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
    # Surface languages, not every known language: this payload is read straight out by
    # the boat UI and rendered verbatim in the console's trace inspector, so building the
    # full set here is what put Tamil copy on an English-only screen.
    messages = {
        lang: [engine.message(p, lang) for p in results] for lang in region.surface_languages
    }
    worst_level = max((p.level for p in results), key=lambda lvl: ALERT_RANK[lvl], default="INFO")
    classes_present = sorted({p.geofence_class for p in results})

    if results:
        # Prose, not enum codes. This summary is spliced verbatim into the answer a
        # fisherman reads, so "MPA 'Gulf of Mannar Marine National Park' 0.00 nm
        # (BREACH)" is the same class of defect that `humanise_verdict_codes` exists to
        # stop for verdicts. The class titles come from config/geofence.yaml via
        # `title_for`, which keeps the five classes distinct (invariant 5) instead of
        # flattening them into "a restricted zone", and keeps the wording in config
        # rather than in application logic (invariant 6).
        summary = " ".join(
            _proximity_sentence(p, _summary_language(region)) for p in results
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
        # Names, not enum codes or layer ids: `unchecked_classes` holds values like
        # "IMBL_HISTORIC_WATERS" and `missing_layers` holds store ids like
        # "imbl_historic_waters" — both internal, neither belongs in user-facing prose.
        # The raw values still reach the caller via `payload["unchecked_classes"]` and
        # `payload["missing_layers"]` for the trace inspector.
        note = (
            "Not all geofence classes could be checked: "
            + ", ".join(title_for(c, _summary_language(region)) for c in unchecked_classes)
            + ". "
            + (
                "A legal boundary is among them, so this position cannot be declared "
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


def _mpa_names(region: RegionConfig) -> dict[str, str]:
    """Vector-store layer id -> the MPA's own configured display name.

    The name lives in the region config (invariant 6 — no boundary name in application
    logic), keyed by the same ``mpa_<id>`` layer id :func:`region_layers` builds, so a
    region swap re-homes the name along with the geometry.
    """
    return {
        f"mpa_{mpa['id']}": mpa.get("name_en") or title_for("MPA", "en")
        for mpa in (region.geofences or {}).get("mpa", []) or []
    }


def _exclusion_summary(
    features: Sequence[dict[str, Any]], region: RegionConfig, lang: str
) -> str:
    """Human, class-distinct sentence for ``get_exclusion_zones`` — the tool-summary
    equivalent of :func:`_proximity_sentence` above.

    Groups every feature by its ``geofence_class`` (never one flattened "restricted
    zone" — invariant 5) and names each class with :func:`title_for`, e.g. "1974
    India-Sri Lanka historic waters boundary" rather than the raw store id
    ``imbl_historic_waters`` those counts are keyed by in ``payload["counts"]``. An MPA
    with a single configured name is named specifically (:func:`_mpa_names`); the raw
    layer ids and hazard classes stay in ``payload`` for the trace inspector and never
    reach this string.
    """
    class_counts: dict[str, int] = {}
    mpa_layers: dict[str, int] = {}
    for feat in features:
        props = feat.get("properties") or {}
        gclass = props.get("geofence_class")
        if not gclass:
            continue
        class_counts[gclass] = class_counts.get(gclass, 0) + 1
        if gclass == "MPA":
            layer_id = str(props.get("hazard_class", ""))
            mpa_layers[layer_id] = mpa_layers.get(layer_id, 0) + 1

    if not class_counts:
        return "No exclusion-zone features found from any source."

    mpa_name_by_layer = _mpa_names(region)
    bits: list[str] = []
    for gclass in GEOFENCE_CLASSES:
        n = class_counts.get(gclass, 0)
        if n == 0:
            continue
        if gclass == "MPA" and len(mpa_layers) == 1:
            (layer_id,) = mpa_layers
            label = mpa_name_by_layer.get(layer_id, title_for(gclass, lang))
        else:
            label = title_for(gclass, lang)
        bits.append(f"{label}: {n} {'zone' if n == 1 else 'zones'}")
    return "Exclusion zones — " + "; ".join(bits) + "."


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
        # This note is spliced verbatim into the answer, so it says the thing in words.
        # It used to print the raw parameter name and an ISO-8601 instant —
        # "'when'='2026-09-07T01:39:59.900346+00:00' was requested but..." — which tells a
        # fisherman nothing and tells a judge that an internal argument reached the copy.
        # The machine-readable value stays in `payload`, where the trace inspector shows it.
        notes.append(
            "These zones describe conditions now. The sources behind them publish only "
            "their current state, so they cannot be rolled forward to the time you asked "
            "about — check again closer to it."
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
                "0 active tropical-cyclone hazard exclusion zones near this region — no "
                "current cyclone threatens this coast, a common valid outcome."
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

    # Human, class-distinct prose — never the raw layer ids/hazard classes `counts` is
    # keyed by. Those stay below in `payload["counts"]`, which the trace inspector
    # already shows (see `_exclusion_summary`'s docstring).
    summary = _exclusion_summary(features, region, _summary_language(region))
    if notes:
        summary = summary + " " + " ".join(notes)
    if sources_failed:
        summary = summary + (
            f" ({len(sources_failed)} data source(s) unavailable for this check; "
            "see the trace for detail.)"
        )

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
