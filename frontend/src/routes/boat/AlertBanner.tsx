/**
 * Proximity/hazard alert banner. Fed by two interchangeable sources that render through
 * this one component so the online and offline experience look identical (PLAN.md's
 * "flip No signal, alert still fires" beat is about the banner *not changing shape*):
 *   - online: `postGeofenceCheck` (tool 9) against the live position
 *   - offline: `checkGeofencesOffline` (turf, client-side) against cached polygons
 */
import { alertLevelMeta, formatDistanceNm, formatEtaSeconds } from "./format";

export interface ProximityAlertVM {
  key: string;
  level: string; // "INFO" | "WARN" | "CRITICAL" | "BREACH"
  geofenceClass: string;
  distanceNm: number | null;
  etaSeconds: number | null;
  inside: boolean;
  headline: string;
  detail?: string | null;
}

export function AlertBanner({
  alerts,
  offline,
  hasData,
}: {
  alerts: ProximityAlertVM[];
  offline: boolean;
  hasData: boolean;
}) {
  if (!hasData) {
    return (
      <div className="alert-banner alert-banner--muted">
        <span className="alert-banner__dot" />
        <span>Boundary watch not initialised yet{offline ? " — no cached boundaries on this device" : ""}.</span>
      </div>
    );
  }

  if (alerts.length === 0) {
    return (
      <div className="alert-banner alert-banner--clear">
        <span className="alert-banner__dot" />
        <span>No boundary or hazard zone nearby.</span>
        <span className="alert-banner__source">{offline ? "OFFLINE CHECK" : "LIVE CHECK"}</span>
      </div>
    );
  }

  return (
    <div className="alert-stack">
      {alerts.map((a) => {
        const meta = alertLevelMeta(a.level);
        const eta = formatEtaSeconds(a.etaSeconds);
        return (
          <div
            key={a.key}
            className="alert-banner alert-banner--active"
            style={{ borderColor: meta.fg, background: meta.bg }}
          >
            <span className="alert-banner__badge" style={{ background: meta.fg }}>
              {a.level}
            </span>
            <div className="alert-banner__body">
              <div className="alert-banner__headline">{a.headline}</div>
              <div className="alert-banner__meta">
                {a.inside ? "inside boundary now" : formatDistanceNm(a.distanceNm)}
                {eta ? ` · closing ETA ${eta}` : ""}
              </div>
              {a.detail ? <div className="alert-banner__detail">{a.detail}</div> : null}
            </div>
            <span className="alert-banner__source">{offline ? "OFFLINE CHECK" : "LIVE CHECK"}</span>
          </div>
        );
      })}
    </div>
  );
}
