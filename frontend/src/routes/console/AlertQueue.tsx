/**
 * Live alert queue — every active alert from the push loop (geofence approach, hazard,
 * weather, verdict change), worst/unacknowledged first. Acknowledgement is a plain
 * `POST /api/alerts/{id}/ack` — see `useConsoleData.ack`.
 */
import { useMemo, useState } from "react";
import type { Alert, AlertLevel, VesselState } from "@shared/types";
import {
  alertLevelVar,
  formatClock,
  formatDistanceNm,
  formatEtaSeconds,
  formatTimeAgo,
  geofenceClassLabel,
  severityForClass,
  severityVar,
} from "./format";

const LEVEL_RANK: Record<string, number> = { BREACH: 3, CRITICAL: 2, WARN: 1, INFO: 0 };

function rank(level: AlertLevel | string): number {
  return LEVEL_RANK[level] ?? 0;
}

function sortAlerts(alerts: Alert[]): Alert[] {
  return [...alerts].sort((a, b) => {
    const aAcked = a.acknowledged_at != null;
    const bAcked = b.acknowledged_at != null;
    if (aAcked !== bAcked) return aAcked ? 1 : -1;
    const levelDiff = rank(b.level) - rank(a.level);
    if (levelDiff !== 0) return levelDiff;
    const aDist = a.distance_nm ?? Number.POSITIVE_INFINITY;
    const bDist = b.distance_nm ?? Number.POSITIVE_INFINITY;
    if (aDist !== bDist) return aDist - bDist;
    return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
  });
}

const KIND_LABEL: Record<Alert["kind"], string> = {
  geofence: "Geofence",
  hazard: "Hazard",
  weather: "Weather",
  verdict_change: "Verdict change",
};

interface AlertQueueProps {
  alerts: Alert[];
  vessels: VesselState[];
  onAck: (alertId: string, by: string) => Promise<void>;
}

export default function AlertQueue({ alerts, vessels, onAck }: AlertQueueProps) {
  const [operator, setOperator] = useState("console-operator");
  const [ackingId, setAckingId] = useState<string | null>(null);

  const vesselName = useMemo(() => {
    const m = new Map(vessels.map((v) => [v.vessel_id, v.name]));
    return (id: string) => m.get(id) ?? id;
  }, [vessels]);

  const sorted = useMemo(() => sortAlerts(alerts), [alerts]);
  const unackedCount = alerts.filter((a) => a.acknowledged_at == null).length;
  const criticalCount = alerts.filter(
    // shared/types.ts's AlertLevel is narrowed to "WARN" | "CRITICAL", but the backend's
    // real AlertLevel (models.py) also emits "INFO" and "BREACH" — rank() takes any
    // string so this stays correct without fighting the (incomplete) shared type.
    (a) => rank(a.level) >= rank("CRITICAL") && a.acknowledged_at == null,
  ).length;

  async function handleAck(alertId: string) {
    setAckingId(alertId);
    try {
      await onAck(alertId, operator.trim() || "console-operator");
    } finally {
      setAckingId(null);
    }
  }

  return (
    <section className="panel alert-queue" aria-label="Alert queue">
      <header className="panel__header">
        <h2>Alert queue</h2>
        <div className="alert-queue__counts">
          <span className="count-chip count-chip--total">{alerts.length} active</span>
          <span className="count-chip count-chip--unacked">{unackedCount} unacked</span>
          {criticalCount > 0 && (
            <span className="count-chip count-chip--critical">{criticalCount} critical/breach</span>
          )}
        </div>
      </header>
      <div className="alert-queue__operator">
        <label htmlFor="operator-name">Acknowledging as</label>
        <input
          id="operator-name"
          type="text"
          value={operator}
          onChange={(e) => setOperator(e.target.value)}
          spellCheck={false}
        />
      </div>
      <div className="alert-queue__list">
        {sorted.length === 0 && <p className="empty-note">No active alerts.</p>}
        {sorted.map((alert) => {
          const acked = alert.acknowledged_at != null;
          const severity = severityForClass(alert.geofence_class ?? undefined);
          return (
            <article
              key={alert.alert_id}
              className={`alert-row${acked ? " alert-row--acked" : ""}`}
              style={{ borderLeftColor: alertLevelVar(alert.level) }}
            >
              <div className="alert-row__top">
                <span className="badge" style={{ background: alertLevelVar(alert.level) }}>
                  {alert.level}
                </span>
                <span className="alert-row__kind">{KIND_LABEL[alert.kind]}</span>
                <span className="alert-row__vessel">{vesselName(alert.vessel_id)}</span>
                <span className="alert-row__time" title={alert.created_at}>
                  {formatTimeAgo(alert.created_at)}
                </span>
              </div>
              <div className="alert-row__title">{alert.title.en}</div>
              <div className="alert-row__body">{alert.body.en}</div>
              <div className="alert-row__meta">
                {alert.geofence_class && (
                  <span className="badge badge--outline" style={{ borderColor: severityVar(severity) }}>
                    {geofenceClassLabel(alert.geofence_class)}
                  </span>
                )}
                <span>Distance: {formatDistanceNm(alert.distance_nm)}</span>
                <span>ETA: {formatEtaSeconds(alert.eta_seconds)}</span>
                <span>{formatClock(alert.created_at)}</span>
              </div>
              {alert.handoff && (
                <div className="alert-row__handoff">
                  Handoff: {alert.handoff.authority_name}
                  {alert.handoff.contact ? ` — ${alert.handoff.contact}` : ""}
                  {alert.handoff.distance_nm != null ? ` (${formatDistanceNm(alert.handoff.distance_nm)})` : ""}
                </div>
              )}
              <div className="alert-row__footer">
                {acked ? (
                  <span className="alert-row__acked">
                    Acknowledged by {alert.acknowledged_by} · {formatTimeAgo(alert.acknowledged_at)}
                  </span>
                ) : (
                  <button
                    type="button"
                    className="btn btn--ack"
                    disabled={ackingId === alert.alert_id}
                    onClick={() => handleAck(alert.alert_id)}
                  >
                    {ackingId === alert.alert_id ? "Acknowledging…" : "Acknowledge"}
                  </button>
                )}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
