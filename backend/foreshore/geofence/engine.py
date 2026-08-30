"""Geofence proximity engine.

Answers one question, deterministically and without a network: *for this position,
heading and speed, how far am I from each boundary, which class is it, and how long
until I cross it?*

Two properties matter more than anything else here:

* **It is pure geometry.** No LLM, no service call. The same computation runs in the
  push loop on the server and, in the boat UI, client-side from ``navigator.geolocation``
  against cached polygons — which is why a geofence warning still fires with no signal
  10 km offshore, where the hazard push cannot reach.
* **Classes stay distinct.** A 1974 historic-waters line, a 1976 maritime boundary, a
  marine national park and a coral reef produce different lead distances, different
  severities and different words. Collapsing them would throw away the only part a
  fisherman acts on differently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from ..config import GeofenceConfig, RegionConfig, load_geofence_config, load_region
from ..models import (
    AlertLevel,
    GeofenceClass,
    GeofenceProximity,
    Observation,
    Provenance,
    haversine_nm,
    project_position,
    utcnow,
)
from ..store.vectors import Feature, NearestResult, VectorStore
from .classes import (
    LAYER_LABEL,
    class_for_layer,
    format_copy,
    level_for,
    region_layers,
    sort_key,
    spec_for,
    title_for,
)


#: Below this closing rate the "approach" is numerical, not navigational — a boat running
#: parallel to a boundary closes on it very slightly as the meridians converge, and
#: reporting an ETA for that would be noise.
MIN_CLOSING_KN = 0.25

#: An ETA further out than this many projection horizons is not actionable, so it is not
#: shown at all rather than shown as a large number nobody will act on.
ETA_HORIZON_MULTIPLE = 4.0


@dataclass
class DynamicFence:
    """A fence that exists only for as long as the hazard does — cyclone cones, wind
    polygons, high-wave cells. Held in memory, never written to the static store."""

    fence_id: str
    name: str
    geometry: dict
    geofence_class: GeofenceClass = "HAZARD_EXCLUSION"
    provenance: Provenance | None = None
    properties: dict | None = None


class GeofenceEngine:
    """Proximity over the static vector layers plus any dynamic hazard fences."""

    def __init__(
        self,
        store: VectorStore | None = None,
        region: RegionConfig | None = None,
        cfg: GeofenceConfig | None = None,
    ) -> None:
        self.store = store or VectorStore()
        self.region = region or load_region()
        self.cfg = cfg or load_geofence_config()
        self._dynamic: list[DynamicFence] = []

    # -- dynamic fences ----------------------------------------------------------------

    def set_dynamic(self, fences: Sequence[DynamicFence]) -> None:
        self._dynamic = list(fences)

    def add_dynamic(self, fence: DynamicFence) -> None:
        self._dynamic.append(fence)

    def clear_dynamic(self) -> None:
        self._dynamic = []

    @property
    def dynamic(self) -> list[DynamicFence]:
        return list(self._dynamic)

    def dynamic_from_features(
        self,
        features: Iterable[dict],
        *,
        provenance: Provenance | None = None,
        name_key: str = "event_name",
        class_key: str = "hazard_class",
    ) -> list[DynamicFence]:
        """Adapt GeoJSON hazard features (GDACS cones, wave-threshold polygons) to fences."""
        out: list[DynamicFence] = []
        for i, f in enumerate(features):
            props = f.get("properties", {}) or {}
            label = props.get(class_key, "hazard")
            name = props.get(name_key) or props.get("name") or str(label).replace("_", " ")
            out.append(
                DynamicFence(
                    fence_id=f"hazard_{label}_{i}",
                    name=str(name),
                    geometry=f.get("geometry") or f,
                    provenance=provenance,
                    properties=props,
                )
            )
        self._dynamic.extend(out)
        return out

    # -- the computation ---------------------------------------------------------------

    def check(
        self,
        lat: float,
        lon: float,
        heading_deg: float | None = None,
        speed_kn: float | None = None,
        *,
        classes: Sequence[GeofenceClass] | None = None,
        include_info: bool = False,
        max_nm: float | None = None,
    ) -> list[GeofenceProximity]:
        """Distance, bearing, ETA and alert level for every fence in range.

        ``eta_seconds`` is the *closing* ETA: it is only reported when the vessel is
        actually moving toward the fence. A boat drifting parallel to the 1974 line is
        not 4 minutes from crossing it, and saying so would train fishermen to ignore
        the alert.
        """
        results: list[GeofenceProximity] = []
        wanted = set(classes) if classes else None

        for layer_id, gclass in region_layers(self.region).items():
            if wanted and gclass not in wanted:
                continue
            try:
                hits = self.store.nearest(layer_id, lat, lon, n=1, max_nm=max_nm)
            except Exception:      # layer not fetched yet — a missing layer is not a crash
                continue
            for hit in hits:
                results.append(
                    self._proximity(
                        hit, layer_id, gclass, lat, lon, heading_deg, speed_kn
                    )
                )

        for fence in self._dynamic:
            if wanted and fence.geofence_class not in wanted:
                continue
            hit = _nearest_to_geometry(fence, lat, lon)
            if hit is None:
                continue
            results.append(
                self._proximity_dynamic(hit, fence, lat, lon, heading_deg, speed_kn)
            )

        if not include_info:
            results = [r for r in results if r.level != "INFO"]
        results.sort(key=lambda r: sort_key(r.geofence_class, r.level, r.distance_nm))
        return results

    # -- internals ---------------------------------------------------------------------

    def _eta(
        self,
        lat: float,
        lon: float,
        target_lat: float,
        target_lon: float,
        distance_nm: float,
        heading_deg: float | None,
        speed_kn: float | None,
        shape: object | None = None,
    ) -> float | None:
        """Closing ETA in seconds, or None when the vessel is not closing on the fence.

        The track is *sampled*, not endpoint-differenced. A boat on a heading that crosses
        a boundary and keeps going ends the hour further from it than it started, so
        comparing only the projected endpoint would report "not closing" for exactly the
        vessel that is about to be arrested. We walk the projected track, find where it
        first comes closest, and report the time to that point.

        Distance along the track is measured against the fence **geometry**, not against
        the point that happens to be nearest right now: on an oblique approach to a long
        boundary the nearest point slides along the line, and pinning it would overstate
        the ETA badly.

        A boat drifting parallel to the 1974 line is genuinely not minutes from crossing
        it, and saying so would train fishermen to ignore the alert — so a track whose
        closest approach is no nearer than the present distance still returns ``None``.
        """
        measure = _distance_to(shape, target_lat, target_lon)
        if heading_deg is None or speed_kn is None:
            return None
        if speed_kn < self.cfg.min_speed_for_eta_kn:
            return None
        horizon = self.cfg.projection_seconds
        steps = 40
        best_t: float | None = None
        best_d = distance_nm
        for i in range(1, steps + 1):
            t = horizon * i / steps
            p_lat, p_lon = project_position(lat, lon, heading_deg, speed_kn, t)
            d = measure(p_lat, p_lon)
            if d <= 0.0:
                return t                  # the track enters the fence at this step
            if d < best_d:
                best_d, best_t = d, t
            elif best_t is not None and d > best_d:
                break                     # past the closest point of approach
        if best_t is None:
            return None                   # never gets nearer than it is now
        closed_nm = distance_nm - best_d
        if closed_nm <= 1e-6:
            return None
        closing_kn = closed_nm / (best_t / 3600.0)
        if closing_kn < MIN_CLOSING_KN:
            return None                   # converging only by the geometry of the meridians
        eta = (distance_nm / closing_kn) * 3600.0
        if eta > horizon * ETA_HORIZON_MULTIPLE:
            return None                   # too far out to be an actionable ETA
        return eta

    def _proximity(
        self,
        hit: NearestResult,
        layer_id: str,
        gclass: GeofenceClass,
        lat: float,
        lon: float,
        heading_deg: float | None,
        speed_kn: float | None,
    ) -> GeofenceProximity:
        props = hit.feature.properties or {}
        name = self._name_for(props, layer_id, gclass)
        spec = spec_for(gclass, self.cfg)
        warn_nm = float(props.get("warn_nm", spec.warn_nm))
        critical_nm = float(props.get("critical_nm", spec.critical_nm))
        level = level_for(
            gclass, hit.distance_nm, hit.inside, cfg=self.cfg,
            warn_nm=warn_nm, critical_nm=critical_nm,
        )
        eta = self._eta(
            lat, lon, hit.closest_lat, hit.closest_lon, hit.distance_nm, heading_deg,
            speed_kn, shape=hit.feature.shape,
        )
        return GeofenceProximity(
            geofence_id=f"{layer_id}:{hit.feature.feature_key}",
            geofence_class=gclass,
            name=name,
            severity=spec.severity,
            distance_nm=hit.distance_nm,
            bearing_deg=hit.bearing_deg,
            inside=hit.inside,
            eta_seconds=eta,
            level=level,
            provenance=self._layer_provenance(layer_id, props),
            closest_lat=hit.closest_lat,
            closest_lon=hit.closest_lon,
        )

    def _proximity_dynamic(
        self,
        hit: NearestResult,
        fence: DynamicFence,
        lat: float,
        lon: float,
        heading_deg: float | None,
        speed_kn: float | None,
    ) -> GeofenceProximity:
        spec = spec_for(fence.geofence_class, self.cfg)
        level = level_for(fence.geofence_class, hit.distance_nm, hit.inside, cfg=self.cfg)
        eta = self._eta(
            lat, lon, hit.closest_lat, hit.closest_lon, hit.distance_nm, heading_deg,
            speed_kn, shape=hit.feature.shape,
        )
        prov = fence.provenance or self._layer_provenance("hazard_exclusion", {})
        return GeofenceProximity(
            geofence_id=fence.fence_id,
            geofence_class=fence.geofence_class,
            name=fence.name,
            severity=spec.severity,
            distance_nm=hit.distance_nm,
            bearing_deg=hit.bearing_deg,
            inside=hit.inside,
            eta_seconds=eta,
            level=level,
            provenance=prov,
            closest_lat=hit.closest_lat,
            closest_lon=hit.closest_lon,
        )

    def _name_for(self, props: dict, layer_id: str, gclass: GeofenceClass) -> str:
        for key in ("name_en", "name", "line_name", "NAME", "Name", "treaty"):
            if props.get(key):
                return str(props[key])
        if layer_id in LAYER_LABEL:
            return LAYER_LABEL[layer_id]["en"]
        return title_for(gclass, "en", self.cfg)

    def _layer_provenance(self, layer_id: str, props: dict) -> Provenance:
        meta = {}
        try:
            meta = self.store.layer_meta(layer_id) or {}
        except Exception:
            meta = {}
        acquired = meta.get("acquired_at")
        from datetime import datetime

        acquired_dt = (
            datetime.fromisoformat(acquired) if isinstance(acquired, str) else utcnow()
        )
        authority = "VLIZ" if layer_id.startswith("imbl") else "INCOIS"
        if layer_id.startswith("mpa") or layer_id == "user_defined":
            authority = "derived"
        if layer_id == "hazard_exclusion":
            authority = "JRC/GDACS"
        return Provenance(
            source_id=meta.get("source_id", layer_id),
            source_name=f"FORESHORE geofence layer '{layer_id}'",
            authority=authority,  # type: ignore[arg-type]
            url=str(props.get("source_url", "")) or f"local://static/{layer_id}.geojson",
            acquired_at=acquired_dt,
            issued_at=acquired_dt,
            valid_to=None,
            spatial_resolution_m=None,
            notes=(
                f"treaty: {props['treaty']}" if props.get("treaty") else None
            ),
        )

    # -- rendering ---------------------------------------------------------------------

    def message(self, prox: GeofenceProximity, lang: str) -> str:
        return format_copy(
            prox.geofence_class,
            prox.level,
            lang,
            name=prox.name,
            distance_nm=prox.distance_nm,
            eta_seconds=prox.eta_seconds,
            cfg=self.cfg,
        )

    def as_geojson(self, classes: Sequence[GeofenceClass] | None = None) -> dict:
        """Everything the map needs to draw, class-tagged and colour-tagged."""
        wanted = set(classes) if classes else None
        features: list[dict] = []
        for layer_id, gclass in region_layers(self.region).items():
            if wanted and gclass not in wanted:
                continue
            try:
                fc = self.store.as_geojson(layer_id)
            except Exception:
                continue
            spec = spec_for(gclass, self.cfg)
            for f in fc.get("features", []):
                props = dict(f.get("properties") or {})
                props.update(
                    {
                        "geofence_class": gclass,
                        "severity": spec.severity,
                        "colour": spec.colour,
                        "warn_nm": props.get("warn_nm", spec.warn_nm),
                        "critical_nm": props.get("critical_nm", spec.critical_nm),
                        "layer_id": layer_id,
                    }
                )
                features.append({**f, "properties": props})
        for fence in self._dynamic:
            spec = spec_for(fence.geofence_class, self.cfg)
            features.append(
                {
                    "type": "Feature",
                    "geometry": fence.geometry,
                    "properties": {
                        **(fence.properties or {}),
                        "name": fence.name,
                        "geofence_class": fence.geofence_class,
                        "severity": spec.severity,
                        "colour": spec.colour,
                        "layer_id": "hazard_exclusion",
                        "dynamic": True,
                    },
                }
            )
        return {"type": "FeatureCollection", "features": features}


def _distance_to(shape: object | None, fallback_lat: float, fallback_lon: float):
    """Return a callable measuring great-circle nm from a position to a fence.

    Falls back to a fixed target point when no geometry is available, so the caller never
    has to branch.
    """
    if shape is None:
        return lambda plat, plon: haversine_nm(plat, plon, fallback_lat, fallback_lon)

    from shapely.geometry import Point
    from shapely.ops import nearest_points

    polygonal = getattr(shape, "geom_type", "") in ("Polygon", "MultiPolygon")

    def measure(plat: float, plon: float) -> float:
        pt = Point(plon, plat)
        if polygonal and shape.contains(pt):      # type: ignore[union-attr]
            return 0.0
        _, closest = nearest_points(pt, shape)    # type: ignore[arg-type]
        return haversine_nm(plat, plon, closest.y, closest.x)

    return measure


def _nearest_to_geometry(fence: DynamicFence, lat: float, lon: float) -> NearestResult | None:
    """Nearest point on an in-memory dynamic fence.

    Dynamic fences never reach the vector store — they live and die with the hazard — so
    the same distance/inside/bearing computation is done here directly, with the identical
    great-circle measure the store uses so the two are comparable.
    """
    from shapely.geometry import Point, shape as shapely_shape
    from shapely.ops import nearest_points

    from ..models import bearing_deg as _bearing

    try:
        shp = shapely_shape(fence.geometry)
    except Exception:
        return None
    if shp.is_empty:
        return None
    pt = Point(lon, lat)
    inside = shp.geom_type in ("Polygon", "MultiPolygon") and shp.contains(pt)
    if inside:
        c_lat, c_lon, dist_nm, brg = lat, lon, 0.0, None
    else:
        _, closest = nearest_points(pt, shp)
        c_lat, c_lon = closest.y, closest.x
        dist_nm = haversine_nm(lat, lon, c_lat, c_lon)
        brg = _bearing(lat, lon, c_lat, c_lon)
    feature = Feature(
        layer_id="hazard_exclusion",
        feature_key=fence.fence_id,
        properties=dict(fence.properties or {}),
        geometry=fence.geometry,
        source_id=(fence.provenance.source_id if fence.provenance else "hazard_exclusion"),
        acquired_at=(fence.provenance.acquired_at if fence.provenance else utcnow()),
    )
    return NearestResult(
        feature=feature,
        distance_nm=dist_nm,
        bearing_deg=brg,
        inside=inside,
        closest_lat=c_lat,
        closest_lon=c_lon,
    )


__all__ = ["GeofenceEngine", "DynamicFence"]
