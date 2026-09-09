/**
 * Live alert queue — every active alert from the push loop (geofence approach, hazard,
 * weather, verdict change), worst/unacknowledged first. Acknowledgement is a plain
 * `POST /api/alerts/{id}/ack` — see `useConsoleData.ack`.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { broadcastAlert } from "@shared/api";
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
  operator: "Operator",
};

interface AlertQueueProps {
  alerts: Alert[];
  vessels: VesselState[];
  onAck: (alertId: string, by: string) => Promise<void>;
  /** Alerts acknowledged during this console session, newest first. The backend drops an
   *  acknowledged alert from its active set, so it cannot be counted from `alerts`. */
  acknowledged?: Alert[];
  selectedVesselId?: string | null;
  onSelectVessel?: (vesselId: string) => void;
}

export default function AlertQueue({
  alerts,
  vessels,
  onAck,
  acknowledged = [],
  selectedVesselId = null,
  onSelectVessel,
}: AlertQueueProps) {
  const [operator, setOperator] = useState("console-operator");
  const [ackingId, setAckingId] = useState<string | null>(null);
  const [ackError, setAckError] = useState<string | null>(null);
  const [showAcknowledged, setShowAcknowledged] = useState(false);
  const listRef = useRef<HTMLDivElement | null>(null);

  const vesselName = useMemo(() => {
    const m = new Map(vessels.map((v) => [v.vessel_id, v.name]));
    return (id: string) => m.get(id) ?? id;
  }, [vessels]);

  // Selecting a vessel pulls its alerts to the top of the queue rather than filtering the
  // rest away: an operator working one boat still has to see a BREACH raised on another.
  const sorted = useMemo(() => {
    const ordered = sortAlerts(alerts);
    if (!selectedVesselId) return ordered;
    return [
      ...ordered.filter((a) => a.vessel_id === selectedVesselId),
      ...ordered.filter((a) => a.vessel_id !== selectedVesselId),
    ];
  }, [alerts, selectedVesselId]);

  const selectedCount = selectedVesselId
    ? alerts.filter((a) => a.vessel_id === selectedVesselId).length
    : 0;

  // Bring the focused vessel's alerts into view when the selection came from the map.
  useEffect(() => {
    if (!selectedVesselId) return;
    listRef.current?.querySelector(".alert-row--selected")?.scrollIntoView({
      behavior: "smooth",
      block: "nearest",
    });
  }, [selectedVesselId]);

  const unackedCount = alerts.filter((a) => a.acknowledged_at == null).length;
  const criticalCount = alerts.filter(
    // shared/types.ts's AlertLevel is narrowed to "WARN" | "CRITICAL", but the backend's
    // real AlertLevel (models.py) also emits "INFO" and "BREACH" — rank() takes any
    // string so this stays correct without fighting the (incomplete) shared type.
    (a) => rank(a.level) >= rank("CRITICAL") && a.acknowledged_at == null,
  ).length;

  async function handleAck(alertId: string) {
    setAckingId(alertId);
    setAckError(null);
    try {
      await onAck(alertId, operator.trim() || "console-operator");
    } catch (err) {
      // A failed acknowledgement must be visible. Silently swallowing it would leave the
      // operator believing an alert had been taken responsibility for when the server
      // never recorded it — the exact failure this panel exists to prevent.
      setAckError(err instanceof Error ? err.message : String(err));
    } finally {
      setAckingId(null);
    }
  }

  const selectedVesselName = selectedVesselId ? vesselName(selectedVesselId) : null;
  // Every alert the console holds is an open one — the backend removes an alert from its
  // active set the moment it is acknowledged — so "open" is the queue length. Kept as its
  // own name rather than reusing `alerts.length` so the meaning is stated once.
  const openCount = unackedCount;

  return (
    <section className="alert-queue" aria-label="Alert queue">
      {/* Header is sticky: the operator scrolls a long queue but must never lose the
          unacknowledged/critical counts or the name acknowledgements are filed under. */}
      <header className="alert-queue__header">
        <div className="alert-queue__title-row">
          <h2 className="alert-queue__title">Alert queue</h2>
          <span className="alert-queue__total">{vessels.length} vessels tracked</span>
        </div>

        {/* These count ALERTS, not vessels — one boat can raise several at once (a
            boundary approach and a hazard cell), and most boats raise none. The labels
            say so explicitly, because "6" next to a 6-boat fleet invites the wrong
            reading. */}
        <div className="alert-queue__counts">
          <CountBox
            label="Open"
            value={openCount}
            tone={openCount > 0 ? "warn" : "idle"}
            title="Alerts raised and not yet acknowledged by an operator."
          />
          <CountBox
            label="Critical"
            value={criticalCount}
            tone={criticalCount > 0 ? "stop" : "idle"}
            title="Of the open alerts, those at CRITICAL or BREACH level — inside a fence, or about to be."
          />
          <CountBox
            label="Acknowledged"
            value={acknowledged.length}
            tone="ok"
            title="Alerts you have acknowledged since this console was opened."
          />
        </div>

        <p className="alert-queue__explainer">
          Acknowledging records <strong>who</strong> accepted responsibility for an alert and
          <strong> when</strong>, then clears it from this queue. It does not act on the boat — the
          skipper is warned by the push path regardless. If the same hazard recurs, it raises a
          fresh alert.
        </p>

        {selectedVesselName && (
          <button
            type="button"
            className="alert-queue__focus"
            onClick={() => selectedVesselId && onSelectVessel?.(selectedVesselId)}
            title="Clear the vessel focus and return to the whole-fleet queue"
          >
            <span className="alert-queue__focus-label">Focused</span>
            <span className="alert-queue__focus-name">{selectedVesselName}</span>
            <span className="alert-queue__focus-count">
              {selectedCount} alert{selectedCount === 1 ? "" : "s"}
            </span>
            <span className="alert-queue__focus-clear" aria-hidden="true">
              ✕
            </span>
          </button>
        )}

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

        {ackError && (
          <p className="alert-queue__ack-error" role="alert">
            Acknowledgement failed — {ackError}. The alert is still open.
          </p>
        )}
      </header>

      <BroadcastComposer vessels={vessels} selectedVesselId={selectedVesselId} />

      <div className="alert-queue__list" ref={listRef}>
        {sorted.length === 0 && <p className="alert-queue__empty">No active alerts.</p>}
        {selectedVesselName && selectedCount === 0 && (
          <p className="alert-queue__empty alert-queue__empty--focus">
            No active alerts for {selectedVesselName}.
          </p>
        )}
        {sorted.map((alert) => {
          const acked = alert.acknowledged_at != null;
          const severity = severityForClass(alert.geofence_class ?? undefined);
          const levelColor = alertLevelVar(alert.level);
          const selected = alert.vessel_id === selectedVesselId;
          return (
            <article
              key={alert.alert_id}
              className={`alert-row${acked ? " alert-row--acked" : ""}${
                selected ? " alert-row--selected" : ""
              }${selectedVesselId && !selected ? " alert-row--dimmed" : ""}`}
              style={{ ["--alert-level-color" as string]: levelColor }}
              // The whole card is the click target for focusing its vessel — the header
              // strip alone would be a smaller hit area than the thing it selects. The
              // Acknowledge button stops propagation so acknowledging never also moves
              // the map.
              onClick={() => onSelectVessel?.(alert.vessel_id)}
            >
              {/* Severity rail — colour is load-bearing here (CLAUDE.md), but it is
                  paired with the level word in the badge below, never colour alone. */}
              <span className="alert-row__rail" aria-hidden="true" />

              <div className="alert-row__top">
                <span className="alert-row__level">{alert.level}</span>
                <span className="alert-row__kind">{KIND_LABEL[alert.kind]}</span>
                <span className="alert-row__time" title={alert.created_at}>
                  {formatTimeAgo(alert.created_at)}
                </span>
              </div>

              <div className="alert-row__vessel">
                <span className="alert-row__vessel-dot" aria-hidden="true" />
                {vesselName(alert.vessel_id)}
              </div>
              <h3 className="alert-row__title">{alert.title.en}</h3>
              <p className="alert-row__body">{alert.body.en}</p>

              {alert.geofence_class && (
                <div className="alert-row__class" style={{ ["--severity-color" as string]: severityVar(severity) }}>
                  <span className="alert-row__class-dot" aria-hidden="true" />
                  {geofenceClassLabel(alert.geofence_class)}
                </div>
              )}

              <dl className="alert-row__metrics">
                <div className="alert-metric">
                  <dt>Distance</dt>
                  <dd>{formatDistanceNm(alert.distance_nm)}</dd>
                </div>
                <div className="alert-metric">
                  <dt>ETA</dt>
                  <dd>{formatEtaSeconds(alert.eta_seconds)}</dd>
                </div>
                <div className="alert-metric">
                  <dt>Raised</dt>
                  <dd>{formatClock(alert.created_at)}</dd>
                </div>
              </dl>

              {alert.handoff && (
                <div className="alert-row__handoff">
                  <span className="alert-row__handoff-label">Handoff</span>
                  <span className="alert-row__handoff-name">{alert.handoff.authority_name}</span>
                  {alert.handoff.contact && (
                    <span className="alert-row__handoff-contact">{alert.handoff.contact}</span>
                  )}
                  {alert.handoff.distance_nm != null && (
                    <span className="alert-row__handoff-dist">{formatDistanceNm(alert.handoff.distance_nm)}</span>
                  )}
                </div>
              )}

              <div className="alert-row__footer">
                {acked ? (
                  <span className="alert-row__acked">
                    Acknowledged by <strong>{alert.acknowledged_by}</strong> · {formatTimeAgo(alert.acknowledged_at)}
                  </span>
                ) : (
                  <button
                    type="button"
                    className="alert-row__ack"
                    disabled={ackingId === alert.alert_id}
                    title={`Record ${operator.trim() || "console-operator"} as having accepted this alert, and clear it from the queue.`}
                    onClick={(e) => {
                      e.stopPropagation();
                      handleAck(alert.alert_id);
                    }}
                  >
                    {ackingId === alert.alert_id ? "Acknowledging…" : "Acknowledge"}
                  </button>
                )}
              </div>
            </article>
          );
        })}
      </div>

      {/* Acknowledged log. Without it, acknowledging is indistinguishable from deleting:
          the row leaves the queue and nothing shows the operator that a named person and
          a timestamp were recorded against it. */}
      {acknowledged.length > 0 && (
        <div className="alert-ack-log">
          <button
            type="button"
            className="alert-ack-log__toggle"
            aria-expanded={showAcknowledged}
            onClick={() => setShowAcknowledged((open) => !open)}
          >
            <span className="alert-ack-log__title">Acknowledged this session</span>
            <span className="alert-ack-log__count">{acknowledged.length}</span>
            <span className={`alert-ack-log__chevron${showAcknowledged ? " alert-ack-log__chevron--open" : ""}`} aria-hidden="true" />
          </button>
          {showAcknowledged && (
            <ul className="alert-ack-log__list">
              {acknowledged.map((alert) => (
                <li key={alert.alert_id} className="alert-ack-log__row">
                  <span className="alert-ack-log__level" style={{ color: alertLevelVar(alert.level) }}>
                    {alert.level}
                  </span>
                  <span className="alert-ack-log__vessel">{vesselName(alert.vessel_id)}</span>
                  <span className="alert-ack-log__what">{alert.title.en}</span>
                  <span className="alert-ack-log__by">
                    {alert.acknowledged_by} · {formatClock(alert.acknowledged_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

/**
 * The console-to-boat push button — see `docs/API.md`'s `POST /api/alerts/broadcast`.
 * Composes one alert, sends it to one vessel or the whole tracked fleet, and it reaches
 * every subscribed client (boat UI, another console, a phone running either) over the
 * live `WS /ws/alerts` socket within the same tick this console's own queue updates —
 * no separate wiring needed for it to show up above, since `useConsoleData` already
 * upserts every pushed "alert" message into `alerts`.
 */
function BroadcastComposer({
  vessels,
  selectedVesselId,
}: {
  vessels: VesselState[];
  selectedVesselId: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState<string>("");
  const [level, setLevel] = useState<AlertLevel>("WARN");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (selectedVesselId) setTarget(selectedVesselId);
  }, [selectedVesselId]);

  async function handleSend() {
    if (!title.trim() || !body.trim() || sending) return;
    setSending(true);
    setError(null);
    setResult(null);
    try {
      const res = await broadcastAlert({
        vessel_id: target || null,
        level,
        title: title.trim(),
        body: body.trim(),
      });
      const targetLabel = target
        ? vessels.find((v) => v.vessel_id === target)?.name ?? target
        : `all ${res.sent} tracked vessel${res.sent === 1 ? "" : "s"}`;
      setResult(`Sent to ${targetLabel}.`);
      setTitle("");
      setBody("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="alert-broadcast">
      <button
        type="button"
        className="alert-broadcast__toggle"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="alert-broadcast__title">Broadcast an alert</span>
        <span className="alert-broadcast__hint">Push it live to the fleet's screens now</span>
      </button>
      {open && (
        <div className="alert-broadcast__form">
          <div className="alert-broadcast__row">
            <label>
              To
              <select value={target} onChange={(e) => setTarget(e.target.value)}>
                <option value="">All tracked vessels</option>
                {vessels.map((v) => (
                  <option key={v.vessel_id} value={v.vessel_id}>
                    {v.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Level
              <select value={level} onChange={(e) => setLevel(e.target.value as AlertLevel)}>
                <option value="INFO">INFO</option>
                <option value="WARN">WARN</option>
                <option value="CRITICAL">CRITICAL</option>
                <option value="BREACH">BREACH</option>
              </select>
            </label>
          </div>
          <input
            type="text"
            placeholder="Title — e.g. Port closed at Rameswaram"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            maxLength={120}
          />
          <textarea
            placeholder="Message — what the crew needs to do"
            value={body}
            onChange={(e) => setBody(e.target.value)}
            rows={2}
            maxLength={400}
          />
          <div className="alert-broadcast__row alert-broadcast__row--actions">
            <button
              type="button"
              className="btn btn--primary"
              disabled={sending || !title.trim() || !body.trim()}
              onClick={handleSend}
            >
              {sending ? "Sending…" : "Send now"}
            </button>
            {result && <span className="alert-broadcast__result">{result}</span>}
            {error && <span className="alert-broadcast__result alert-broadcast__result--error">{error}</span>}
          </div>
        </div>
      )}
    </div>
  );
}

function CountBox({
  label,
  value,
  tone,
  title,
}: {
  label: string;
  value: number;
  tone: "warn" | "stop" | "ok" | "idle";
  title?: string;
}) {
  return (
    <div className={`count-box count-box--${tone}`} title={title}>
      <span className="count-box__value">{value}</span>
      <span className="count-box__label">{label}</span>
    </div>
  );
}
