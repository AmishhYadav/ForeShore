"""Geofence classes — five, semantically distinct, plus one dynamic hazard class.

The distinction *is* the point. A 1974 historic-waters boundary is a different legal
regime from a 1976 maritime boundary; a marine national park is a conservation
restriction and not a national border at all; an eco-sensitive habitat is advisory.
Collapsing them into one "restricted zone" type would throw away the only part of this
that a fisherman actually needs to act on differently.

Class definitions, lead distances and bilingual copy all live in ``config/geofence.yaml``
so that a region swap re-homes the wording as well as the geometry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from ..config import GeofenceConfig, RegionConfig, load_geofence_config, load_region
from ..models import AlertLevel, GeofenceClass, Severity

#: Every class this system knows. Ordered by how hard the consequence is.
GEOFENCE_CLASSES: tuple[GeofenceClass, ...] = (
    "IMBL_HISTORIC_WATERS",
    "IMBL_MARITIME_BOUNDARY",
    "HAZARD_EXCLUSION",
    "MPA",
    "ECO_SENSITIVE",
    "USER_DEFINED",
)

#: Layer id in the vector store -> geofence class. Layers are written by
#: scripts/fetch_static.py; the mapping is here so the store stays dumb.
LAYER_CLASS: dict[str, GeofenceClass] = {
    "imbl_historic_waters": "IMBL_HISTORIC_WATERS",
    "imbl_maritime_boundary": "IMBL_MARITIME_BOUNDARY",
    "mpa": "MPA",
    "eco_coral": "ECO_SENSITIVE",
    "eco_seagrass": "ECO_SENSITIVE",
    "eco_mangrove": "ECO_SENSITIVE",
    "user_defined": "USER_DEFINED",
    "hazard_exclusion": "HAZARD_EXCLUSION",
}

#: Human-facing habitat names, so ECO_SENSITIVE copy can say what the habitat is.
LAYER_LABEL: dict[str, dict[str, str]] = {
    "eco_coral": {"en": "coral reef", "ta": "பவளப்பாறை"},
    "eco_seagrass": {"en": "seagrass bed", "ta": "கடற்புல் படுகை"},
    "eco_mangrove": {"en": "mangrove", "ta": "சதுப்புநிலக் காடு"},
}

SEVERITY_RANK: dict[Severity, int] = {
    "legal_hard": 3,
    "hazard": 2,
    "restricted": 1,
    "advisory": 0,
}

ALERT_RANK: dict[AlertLevel, int] = {"INFO": 0, "WARN": 1, "CRITICAL": 2, "BREACH": 3}


@dataclass(frozen=True)
class ClassSpec:
    """Resolved definition of one geofence class for one region."""

    geofence_class: GeofenceClass
    severity: Severity
    warn_nm: float
    critical_nm: float
    colour: str
    title: dict[str, str]

    @property
    def is_legal(self) -> bool:
        return self.severity == "legal_hard"

    @property
    def blocks_routing(self) -> bool:
        """Classes the router must treat as impassable, not merely expensive."""
        return self.severity in ("legal_hard", "hazard")


def spec_for(
    geofence_class: GeofenceClass, cfg: GeofenceConfig | None = None
) -> ClassSpec:
    cfg = cfg or load_geofence_config()
    copy = cfg.classes.get(geofence_class)
    if copy is None:
        raise KeyError(
            f"geofence class {geofence_class!r} is not defined in config/geofence.yaml; "
            f"known: {sorted(cfg.classes)}"
        )
    return ClassSpec(
        geofence_class=geofence_class,
        severity=copy.severity,  # type: ignore[arg-type]
        warn_nm=copy.warn_nm,
        critical_nm=copy.critical_nm,
        colour=copy.colour,
        title=copy.title,
    )


def class_for_layer(layer_id: str) -> GeofenceClass | None:
    return LAYER_CLASS.get(layer_id)


def level_for(
    geofence_class: GeofenceClass,
    distance_nm: float,
    inside: bool,
    *,
    cfg: GeofenceConfig | None = None,
    warn_nm: float | None = None,
    critical_nm: float | None = None,
) -> AlertLevel:
    """Alert level from distance. Overrides let USER_DEFINED fences carry their own."""
    spec = spec_for(geofence_class, cfg)
    warn = spec.warn_nm if warn_nm is None else warn_nm
    crit = spec.critical_nm if critical_nm is None else critical_nm
    if inside:
        return "BREACH"
    if distance_nm <= crit:
        return "CRITICAL"
    if distance_nm <= warn:
        return "WARN"
    return "INFO"


def format_copy(
    geofence_class: GeofenceClass,
    level: AlertLevel,
    lang: str,
    *,
    name: str = "",
    distance_nm: float | None = None,
    eta_seconds: float | None = None,
    cfg: GeofenceConfig | None = None,
) -> str:
    """Render the class's copy for a level and language.

    Copy is per class and per language and is never interchangeable — that is exactly the
    distinction the classes exist to preserve.
    """
    cfg = cfg or load_geofence_config()
    copy = cfg.classes[geofence_class]
    template = copy.text(level if level != "INFO" else "WARN", lang)
    return template.format(
        name=name or copy.title.get(lang) or copy.title.get("en", ""),
        distance=("?" if distance_nm is None else f"{distance_nm:.1f}"),
        eta=format_eta(eta_seconds, lang),
    )


def title_for(geofence_class: GeofenceClass, lang: str, cfg: GeofenceConfig | None = None) -> str:
    cfg = cfg or load_geofence_config()
    t = cfg.classes[geofence_class].title
    return t.get(lang) or t.get("en", geofence_class)


def format_eta(seconds: float | None, lang: str = "en") -> str:
    """Spoken-friendly ETA. Kept short because it is read aloud over an engine."""
    if seconds is None or seconds <= 0:
        return {"ta": "விரைவில்", "en": "shortly"}.get(lang, "shortly")
    minutes = int(round(seconds / 60.0))
    if minutes < 1:
        return {"ta": "ஒரு நிமிடத்திற்குள்", "en": "under a minute"}.get(lang, "under a minute")
    if minutes < 60:
        return {"ta": f"{minutes} நிமிடம்", "en": f"{minutes} min"}.get(lang, f"{minutes} min")
    hours = minutes / 60.0
    return {"ta": f"{hours:.1f} மணி", "en": f"{hours:.1f} h"}.get(lang, f"{hours:.1f} h")


def sort_key(geofence_class: GeofenceClass, level: AlertLevel, distance_nm: float) -> tuple:
    """Ordering for the alert queue: worst level, then hardest severity, then nearest."""
    spec = spec_for(geofence_class)
    return (-ALERT_RANK[level], -SEVERITY_RANK[spec.severity], distance_nm)


def region_layers(region: RegionConfig | None = None) -> dict[str, GeofenceClass]:
    """Layers that exist for this region, including region-declared MPAs.

    Nothing here is hardcoded per region: the MPA entries come from the region file.
    """
    region = region or load_region()
    layers = dict(LAYER_CLASS)
    for mpa in (region.geofences or {}).get("mpa", []) or []:
        layers[f"mpa_{mpa['id']}"] = "MPA"
    layers.pop("mpa", None)
    return layers


def describe_classes(lang: str = "en") -> list[dict]:
    """Legend payload for both UIs — one row per class with its lead distances."""
    cfg = load_geofence_config()
    out = []
    for gc in GEOFENCE_CLASSES:
        if gc not in cfg.classes:
            continue
        spec = spec_for(gc, cfg)
        out.append(
            {
                "geofence_class": gc,
                "title": title_for(gc, lang, cfg),
                "severity": spec.severity,
                "warn_nm": spec.warn_nm,
                "critical_nm": spec.critical_nm,
                "colour": spec.colour,
                "blocks_routing": spec.blocks_routing,
            }
        )
    return out


__all__ = [
    "GEOFENCE_CLASSES", "LAYER_CLASS", "LAYER_LABEL", "SEVERITY_RANK", "ALERT_RANK",
    "ClassSpec", "spec_for", "class_for_layer", "level_for", "format_copy", "title_for",
    "format_eta", "sort_key", "region_layers", "describe_classes",
]
