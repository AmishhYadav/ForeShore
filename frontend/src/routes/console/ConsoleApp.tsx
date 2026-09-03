/**
 * Shore-side control console — PLAN.md Phase 6. English, information-dense, built for
 * an operator watching a whole simulated fleet through a cyclone, not a single glance
 * from a phone (contrast with the boat UI's giant single-verdict-card focus).
 *
 * Layout: fleet map + alert queue dominate (left column full height / top of the right
 * column); trace inspector, analyst query and the architecture panel live in a tabbed
 * drawer beneath the alert queue — PLAN.md explicitly allows those three as "secondary
 * panels/drawers".
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

type Tab = "query" | "trace" | "architecture" | "region";

/** RegionInfo.basemap is an opaque `Record<string, unknown>` in shared/types.ts (a live
 * backend config blob, not enumerated field-by-field there) — this narrows just the two
 * fields FleetMap's optional `center`/`zoom` recentre prop needs, mirroring FleetMap.tsx's
 * own local `Basemap` interface rather than exporting/importing one across files. */
interface BasemapCenterZoom {
  center?: [number, number];
  zoom?: number;
}

export default function ConsoleApp() {
  const data = useConsoleData();
  const [tab, setTab] = useState<Tab>("query");
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null);
  // Evidence panels are only ever produced by a fresh POST /api/query response, so this
  // is a session-local cache keyed by query_id — populated as AnalystQuery answers
  // queries, read by TraceInspector to join provenance ids against real records. See
  // TraceInspector.tsx's module docstring for why older/persisted traces won't have an
  // entry here.
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
  // Recentre target for FleetMap after a region swap — reactive to data.region because
  // useConsoleData.swapRegion already calls setRegion internally, so this recomputes and
  // re-renders on its own the moment RegionSwitcher's onSwap resolves; no extra plumbing
  // needed between RegionSwitcher and the map.
  const basemap = data.region?.basemap as BasemapCenterZoom | undefined;

  return (
    <div className="console-shell">
      <header className="console-header">
        <div className="console-header__title">
          <span className="console-header__brand">FORESHORE</span>
          <span className="console-header__sub">Shore Control Console</span>
        </div>
        <div className="console-header__stats">
          <StatusChip label={data.wsConnected ? "LIVE" : "OFFLINE — polling"} ok={data.wsConnected} />
          {data.wsHello && <span className="console-header__stat">mode: {data.wsHello.mode}</span>}
          <span className="console-header__stat">
            {data.region?.display_name_en ?? "—"}
          </span>
          <span className="console-header__stat">{data.vessels.length} vessels tracked</span>
          <span className="console-header__stat">{activeAlerts} unacknowledged alerts</span>
          <span className="console-header__stat">
            fleet updated {formatTimeAgo(data.vesselsUpdatedAt)}
          </span>
        </div>
      </header>

      {data.loadError && (
        <div className="console-banner console-banner--error">
          Initial load failed for one or more panels: {data.loadError}. Retrying via the periodic poll.
        </div>
      )}

      <main className="console-main">
        <section className="console-map-pane" aria-label="Fleet map">
          <FleetMap
            region={data.region}
            vessels={data.vessels}
            geofences={data.geofences}
            center={basemap?.center}
            zoom={basemap?.zoom}
          />
        </section>

        <section className="console-side-pane">
          <div className="console-side-pane__alerts">
            <AlertQueue alerts={data.alerts} vessels={data.vessels} onAck={data.ack} />
          </div>

          <div className="console-side-pane__drawer">
            <nav className="console-tabbar" aria-label="Secondary panels">
              <button
                type="button"
                className={`console-tabbar__btn${tab === "query" ? " console-tabbar__btn--active" : ""}`}
                onClick={() => setTab("query")}
              >
                Analyst query
              </button>
              <button
                type="button"
                className={`console-tabbar__btn${tab === "trace" ? " console-tabbar__btn--active" : ""}`}
                onClick={() => setTab("trace")}
              >
                Trace inspector
              </button>
              <button
                type="button"
                className={`console-tabbar__btn${tab === "architecture" ? " console-tabbar__btn--active" : ""}`}
                onClick={() => setTab("architecture")}
              >
                Architecture
              </button>
              <button
                type="button"
                className={`console-tabbar__btn${tab === "region" ? " console-tabbar__btn--active" : ""}`}
                onClick={() => setTab("region")}
              >
                Region swap
              </button>
            </nav>
            <div className="console-tabpanel">
              {tab === "query" && (
                <AnalystQuery onQueryComplete={handleQueryComplete} onViewTrace={viewTrace} />
              )}
              {tab === "trace" && (
                <TraceInspector
                  traces={data.traces}
                  selectedId={selectedTraceId}
                  onSelect={setSelectedTraceId}
                  evidencePanel={selectedTraceId ? evidenceByQuery[selectedTraceId] : null}
                />
              )}
              {tab === "architecture" && <ArchitecturePanel specialists={data.architecture} />}
              {tab === "region" && (
                <RegionSwitcher currentRegion={data.region} onSwap={data.swapRegion} />
              )}
            </div>
          </div>
        </section>
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
