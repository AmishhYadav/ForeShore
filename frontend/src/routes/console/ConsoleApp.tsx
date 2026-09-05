/**
 * Shore-side control console — PLAN.md Phase 6. English, information-dense, built for
 * an operator watching a whole simulated fleet through a cyclone, not a single glance
 * from a phone (contrast with the boat UI's giant single-verdict-card focus).
 *
 * Now organised into 4 top-navigation tabs:
 *   - Fleet: FleetMap + AlertQueue
 *   - Query: AnalystQuery
 *   - Traces: TraceInspector
 *   - System: ArchitecturePanel + RegionSwitcher
 *
 * All data fetching and WS wiring lives in ./useConsoleData — this file is render/layout
 * only, and calls the backend exclusively through frontend/src/shared/api.ts + ws.ts.
 */
import { useState } from "react";
import "./console.css";
import FleetMap from "./FleetMap";
import AlertQueue from "./AlertQueue";
import TraceInspector from "./TraceInspector";
import AnalystQuery from "./AnalystQuery";
import ArchitecturePanel from "./ArchitecturePanel";
import RegionSwitcher from "./RegionSwitcher";
import { useConsoleData } from "./useConsoleData";
import { formatTimeAgo } from "./format";
import type { EvidencePanelRow, QueryOutcome } from "@shared/types";

type Tab = "fleet" | "query" | "trace" | "system";

/** RegionInfo.basemap is an opaque `Record<string, unknown>` in shared/types.ts */
interface BasemapCenterZoom {
  center?: [number, number];
  zoom?: number;
}

export default function ConsoleApp() {
  const data = useConsoleData();
  const [tab, setTab] = useState<Tab>("fleet");
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null);
  const [evidenceByQuery, setEvidenceByQuery] = useState<Record<string, EvidencePanelRow[]>>({});

  function viewTrace(queryId: string) {
    setSelectedTraceId(queryId);
    setTab("trace");
  }

  function handleQueryComplete(outcome: QueryOutcome) {
    data.refreshTraces();
    setSelectedTraceId(outcome.query_id);
    setEvidenceByQuery((prev) => ({ ...prev, [outcome.query_id]: outcome.payloads.evidence_panel }));
  }

  const activeAlerts = data.alerts.filter((a) => a.acknowledged_at == null).length;
  const basemap = data.region?.basemap as BasemapCenterZoom | undefined;

  return (
    <div className="console-shell">
      {/* ── Header ────────────────────────────────────────── */}
      <header className="console-header">
        <div className="console-header__left">
          <div className="console-header__brand-mark" />
          <div className="console-header__title-group">
            <span className="console-header__brand">FORESHORE</span>
            <span className="console-header__sub">Shore Control Console</span>
          </div>
        </div>
        <div className="console-header__stats">
          <StatusChip label={data.wsConnected ? "LIVE" : "OFFLINE"} ok={data.wsConnected} />
          {data.wsHello && <span className="console-header__stat">{data.wsHello.mode}</span>}
          <span className="console-header__stat">{data.region?.display_name_en ?? "—"}</span>
          <span className="console-header__stat">{data.vessels.length} vessels</span>
          <span className="console-header__stat console-header__stat--alert">
            {activeAlerts} alerts
          </span>
          <span className="console-header__stat console-header__stat--time">
            {formatTimeAgo(data.vesselsUpdatedAt)}
          </span>
        </div>
      </header>

      {/* ── Top Navigation Tabs ───────────────────────────── */}
      <nav className="console-tabs" aria-label="Main navigation">
        {(
          [
            { id: "fleet", label: "Fleet", icon: "🗺️" },
            { id: "query", label: "Query", icon: "💬" },
            { id: "trace", label: "Traces", icon: "🔍" },
            { id: "system", label: "System", icon: "⚙️" },
          ] as { id: Tab; label: string; icon: string }[]
        ).map((t) => (
          <button
            key={t.id}
            type="button"
            className={`console-tabs__tab${tab === t.id ? " console-tabs__tab--active" : ""}`}
            onClick={() => setTab(t.id)}
          >
            <span className="console-tabs__icon">{t.icon}</span>
            <span className="console-tabs__label">{t.label}</span>
          </button>
        ))}
      </nav>

      {data.loadError && (
        <div className="console-banner console-banner--error">
          Initial load failed: {data.loadError}. Retrying via the periodic poll.
        </div>
      )}

      {/* ── Tab Content ───────────────────────────────────── */}
      <main className="console-main">
        {/* Fleet Tab */}
        {tab === "fleet" && (
          <div className="console-fleet-layout animate-fade-in">
            <section className="console-map-pane" aria-label="Fleet map">
              <FleetMap
                region={data.region}
                vessels={data.vessels}
                geofences={data.geofences}
                center={basemap?.center}
                zoom={basemap?.zoom}
              />
            </section>
            <section className="console-alerts-pane">
              <AlertQueue alerts={data.alerts} vessels={data.vessels} onAck={data.ack} />
            </section>
          </div>
        )}

        {/* Query Tab */}
        {tab === "query" && (
          <div className="console-query-pane animate-fade-in">
            <AnalystQuery onQueryComplete={handleQueryComplete} onViewTrace={viewTrace} />
          </div>
        )}

        {/* Trace Tab */}
        {tab === "trace" && (
          <div className="console-trace-pane animate-fade-in">
            <TraceInspector
              traces={data.traces}
              selectedId={selectedTraceId}
              onSelect={setSelectedTraceId}
              evidencePanel={selectedTraceId ? evidenceByQuery[selectedTraceId] : null}
            />
          </div>
        )}

        {/* System Tab */}
        {tab === "system" && (
          <div className="console-system-pane animate-fade-in">
            <div className="console-system-grid">
              <div className="console-system-card">
                <h3 className="console-system-card__title">Architecture</h3>
                <ArchitecturePanel specialists={data.architecture} />
              </div>
              <div className="console-system-card">
                <h3 className="console-system-card__title">Region</h3>
                <RegionSwitcher currentRegion={data.region} onSwap={data.swapRegion} />
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

function StatusChip({ label, ok }: { label: string; ok: boolean }) {
  return (
    <span className={`status-chip${ok ? " status-chip--ok" : " status-chip--warn"}`}>
      <span className="status-chip__dot" />
      {label}
    </span>
  );
}
