/**
 * Live push-path alerts over `WS /ws/alerts` — a console broadcast
 * (`POST /api/alerts/broadcast`) and the fleet loop's own weather/hazard/geofence
 * alerts. `AlertQueue.tsx`'s console already renders this transport; this hook is what
 * was missing on the boat side — `BoatApp.tsx` never opened the socket at all, so
 * nothing the push path (or a console's manual broadcast) sent could ever reach this
 * screen. Mapped into `ProximityAlertVM` so it renders through the existing
 * `AlertBanner` with no new UI shape.
 */
import { useEffect, useState } from "react";
import { AlertSocket } from "@shared/ws";
import type { Alert } from "@shared/types";
import type { ProximityAlertVM } from "./AlertBanner";

//: Most recent first, capped so a burst of pushes cannot fill the screen.
const MAX_PUSH_ALERTS = 3;

function toVm(a: Alert): ProximityAlertVM {
  return {
    key: a.alert_id,
    level: a.level,
    geofenceClass: a.geofence_class ?? a.kind.toUpperCase(),
    distanceNm: a.distance_nm,
    etaSeconds: a.eta_seconds,
    inside: false,
    headline: a.title.en,
    detail: a.body.en,
  };
}

/**
 * `vesselId` scopes the subscription to one tracked vessel (a demo boat identifying
 * itself as, say, `FS-01` via `?vessel_id=FS-01` in the URL); omitted/`null` subscribes
 * to every vessel, so any broadcast — targeted or fleet-wide — reaches this screen.
 */
export function usePushAlerts(vesselId?: string | null): ProximityAlertVM[] {
  const [alerts, setAlerts] = useState<Alert[]>([]);

  useEffect(() => {
    const socket = new AlertSocket();
    socket.connect();
    socket.subscribe(vesselId ? [vesselId] : []);
    const unsubscribe = socket.onMessage((msg) => {
      if (msg.type !== "alert") return;
      setAlerts((prev) => [msg.alert, ...prev.filter((a) => a.alert_id !== msg.alert.alert_id)].slice(0, MAX_PUSH_ALERTS));
    });
    return () => {
      unsubscribe();
      socket.disconnect();
    };
  }, [vesselId]);

  return alerts.map(toVm);
}
