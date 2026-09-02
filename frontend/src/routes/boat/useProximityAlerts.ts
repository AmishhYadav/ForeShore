/**
 * Feeds `AlertBanner` from whichever source matches the current connectivity state, so
 * flipping "No signal" swaps the data source but never the shape on screen:
 *   - offline: `checkGeofencesOffline` (turf, client-side, cached polygons)
 *   - online: `postGeofenceCheck` (tool 9), polled — never more than every ~15s, so the
 *     boat's own position doesn't hammer the backend the way the push loop does for the
 *     tracked fleet.
 */
import { useEffect, useRef, useState } from "react";
import { postGeofenceCheck } from "@shared/api";
import { checkGeofencesOffline } from "@shared/geofenceCheck";
import type { ProximityAlertVM } from "./AlertBanner";
import type { OwnPosition } from "./useOwnPosition";

const LEVEL_RANK: Record<string, number> = { BREACH: 3, CRITICAL: 2, WARN: 1, INFO: 0 };

function sortAlerts(alerts: ProximityAlertVM[]): ProximityAlertVM[] {
  return [...alerts]
    .sort((a, b) => {
      const rank = (LEVEL_RANK[b.level] ?? 0) - (LEVEL_RANK[a.level] ?? 0);
      if (rank !== 0) return rank;
      return (a.distanceNm ?? Infinity) - (b.distanceNm ?? Infinity);
    })
    .slice(0, 3);
}

export function useProximityAlerts({
  offline,
  position,
  geofenceGeoJson,
  language,
}: {
  offline: boolean;
  position: OwnPosition;
  geofenceGeoJson: GeoJSON.FeatureCollection | null;
  language: string;
}): { alerts: ProximityAlertVM[]; hasData: boolean } {
  const [alerts, setAlerts] = useState<ProximityAlertVM[]>([]);
  const [hasCheckedOnline, setHasCheckedOnline] = useState(false);
  const lastCallRef = useRef(0);

  // -- offline: recompute synchronously whenever inputs change --------------------------
  useEffect(() => {
    if (!offline) return;
    if (!position.ready || !geofenceGeoJson) {
      setAlerts([]);
      return;
    }
    const results = checkGeofencesOffline(geofenceGeoJson, position.lat, position.lon, {
      headingDeg: position.headingDeg ?? undefined,
      speedKn: position.speedKn ?? undefined,
    });
    setAlerts(
      sortAlerts(
        results.map((r) => ({
          key: `${r.layer_id}-${r.geofence_class}`,
          level: r.level,
          geofenceClass: r.geofence_class,
          distanceNm: r.distance_nm,
          etaSeconds: r.eta_seconds,
          inside: r.inside,
          headline: (r.title as Record<string, string>)[language] || r.title.en || r.name,
        })),
      ),
    );
  }, [offline, position.ready, position.lat, position.lon, position.headingDeg, position.speedKn, geofenceGeoJson, language]);

  // -- online: poll tool 9 directly ------------------------------------------------------
  useEffect(() => {
    if (offline || !position.ready) return;
    let cancelled = false;

    async function run() {
      const now = Date.now();
      if (now - lastCallRef.current < 15000) return;
      lastCallRef.current = now;
      try {
        const res = await postGeofenceCheck({
          lat: position.lat,
          lon: position.lon,
          heading_deg: position.headingDeg ?? undefined,
          speed_kn: position.speedKn ?? undefined,
        });
        if (cancelled) return;
        const payload = res.payload as Record<string, unknown>;
        const proximities = payload?.proximities;
        const messages = payload?.messages as Record<string, string[]> | undefined;
        const langMessages = messages?.[language] ?? messages?.en ?? [];
        if (Array.isArray(proximities)) {
          setAlerts(
            sortAlerts(
              (proximities as Record<string, unknown>[])
                .filter((p) => p.level && p.level !== "INFO")
                .map((p, i) => ({
                  key: `${String(p.geofence_id ?? p.name ?? i)}`,
                  level: String(p.level ?? "INFO"),
                  geofenceClass: String(p.geofence_class ?? ""),
                  distanceNm: typeof p.distance_nm === "number" ? p.distance_nm : null,
                  etaSeconds: typeof p.eta_seconds === "number" ? p.eta_seconds : null,
                  inside: Boolean(p.inside),
                  headline: String(p.name ?? p.geofence_class ?? "boundary"),
                  detail: langMessages[i] ?? null,
                })),
            ),
          );
        }
      } catch (err) {
        console.warn("[useProximityAlerts] online geofence check failed:", err);
        // Transient network blip — keep showing the last known alerts rather than
        // flashing to "all clear".
      } finally {
        if (!cancelled) setHasCheckedOnline(true);
      }
    }

    run();
    const interval = setInterval(run, 15000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [offline, position.ready, position.lat, position.lon, position.headingDeg, position.speedKn, language]);

  const hasData = offline ? Boolean(geofenceGeoJson) : hasCheckedOnline;
  return { alerts, hasData };
}
