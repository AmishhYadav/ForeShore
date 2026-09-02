/**
 * Client-side geofence proximity check — PLAN.md Phase 5, locked decision:
 * "`geofence/check` runs client-side from `navigator.geolocation` — proximity needs no
 * network." Mirrors the semantics of `foreshore.geofence.engine.GeofenceEngine.check`
 * (backend/foreshore/geofence/engine.py) closely enough that the same vessel position
 * yields the same class/level online or offline, using the same `warn_nm`/`critical_nm`
 * carried on every cached feature (see the `title`/`warn_nm`/`critical_nm` properties
 * `as_geojson()` writes onto every feature).
 *
 * Uses @turf/turf (battle-tested computational geometry) rather than hand-rolled
 * point/polygon math — this is a safety check, not a place to debug distance formulas.
 */
import * as turf from "@turf/turf";
import type { AlertLevel, GeofenceClass } from "./types";

export interface GeofenceProximityOffline {
  geofence_class: GeofenceClass;
  layer_id: string;
  name: string;
  title: { en: string; ta: string };
  severity: string;
  colour: string;
  distance_nm: number;
  inside: boolean;
  level: AlertLevel | "INFO";
  eta_seconds: number | null;
}

const NM_PER_KM = 0.539957;

function levelFor(distanceNm: number, inside: boolean, warnNm: number, critNm: number): AlertLevel | "INFO" {
  if (inside) return "BREACH" as AlertLevel;
  if (distanceNm <= critNm) return "CRITICAL";
  if (distanceNm <= warnNm) return "WARN";
  return "INFO";
}

/**
 * Nearest distance (nm) from a point to a feature's geometry, and whether the point is
 * inside it (only meaningful for polygonal features — MPA, hazard exclusion).
 */
function distanceToFeature(
  point: GeoJSON.Feature<GeoJSON.Point>,
  feature: GeoJSON.Feature,
): { distanceNm: number; inside: boolean } {
  const geom = feature.geometry;
  if (geom.type === "Polygon" || geom.type === "MultiPolygon") {
    const inside = turf.booleanPointInPolygon(point, geom as GeoJSON.Polygon | GeoJSON.MultiPolygon);
    if (inside) return { distanceNm: 0, inside: true };
    // Distance to the nearest edge — turf.polygonToLine gives the boundary as a line.
    const boundary = turf.polygonToLine(feature as GeoJSON.Feature<GeoJSON.Polygon | GeoJSON.MultiPolygon>) as GeoJSON.Feature<
      GeoJSON.LineString | GeoJSON.MultiLineString
    >;
    const nearest = turf.nearestPointOnLine(boundary, point);
    const km = (nearest.properties?.dist as number | undefined) ?? turf.distance(point, nearest, { units: "kilometers" });
    return { distanceNm: km * NM_PER_KM, inside: false };
  }
  if (geom.type === "LineString" || geom.type === "MultiLineString") {
    const nearest = turf.nearestPointOnLine(feature as GeoJSON.Feature<GeoJSON.LineString>, point);
    const km = (nearest.properties?.dist as number | undefined) ?? turf.distance(point, nearest, { units: "kilometers" });
    return { distanceNm: km * NM_PER_KM, inside: false };
  }
  // Point/MultiPoint fallback.
  const km = turf.distance(point, feature as GeoJSON.Feature<GeoJSON.Point>, { units: "kilometers" });
  return { distanceNm: km * NM_PER_KM, inside: false };
}

/**
 * @param headingDeg / speedKn — when given, ETA-to-breach is estimated for fences ahead
 * of the vessel's track (closing distance / speed); omitted for fences not roughly ahead.
 */
export function checkGeofencesOffline(
  cached: GeoJSON.FeatureCollection,
  lat: number,
  lon: number,
  opts?: { headingDeg?: number; speedKn?: number },
): GeofenceProximityOffline[] {
  const point = turf.point([lon, lat]);
  const results: GeofenceProximityOffline[] = [];

  for (const feature of cached.features) {
    const props = (feature.properties ?? {}) as Record<string, unknown>;
    const warnNm = Number(props.warn_nm ?? 0);
    const critNm = Number(props.critical_nm ?? 0);
    const { distanceNm, inside } = distanceToFeature(point, feature);
    const level = levelFor(distanceNm, inside, warnNm, critNm);
    if (level === "INFO") continue; // only report fences actually worth a card

    let etaSeconds: number | null = null;
    if (opts?.speedKn && opts.speedKn > 0 && !inside) {
      // Coarse closing-ETA: straight-line distance / speed. Good enough for a WARN/
      // CRITICAL card; the backend's tool 9 does the bearing-aware version online.
      etaSeconds = (distanceNm / opts.speedKn) * 3600;
    }

    results.push({
      geofence_class: props.geofence_class as GeofenceClass,
      layer_id: String(props.layer_id ?? ""),
      name: String(props.name ?? props.title ?? props.geofence_class ?? ""),
      title: (props.title as { en: string; ta: string }) ?? { en: "", ta: "" },
      severity: String(props.severity ?? ""),
      colour: String(props.colour ?? "#999"),
      distance_nm: Math.round(distanceNm * 100) / 100,
      inside,
      level,
      eta_seconds: etaSeconds,
    });
  }

  results.sort((a, b) => a.distance_nm - b.distance_nm);
  return results;
}
