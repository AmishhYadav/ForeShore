/**
 * Trace inspector — "are those real agents, or five boxes on a slide?"
 *
 * `GET /api/trace/{query_id}` (see backend/foreshore/store/traces.py::TraceStore.tree)
 * returns a nested tree of `TraceTreeNode`s keyed by `parent_id`. `shared/types.ts`'s
 * `TraceStep`/`TraceTreeNode` interfaces have since been corrected to name the field
 * `parent_id` (matching the backend's `TraceStep.to_dict()`) — verified against a live
 * `/api/trace/{id}` response — so this file imports them directly instead of
 * hand-duplicating local types.
 *
 * Each step's `provenance_ids` are bare `"<source_id>@<issued_at-or-acquired_at>"` keys
 * (models.py's `Provenance.provenance_id`). To render the actual provenance record
 * (source name, authority, acquisition time, freshness, resolution) rather than the raw
 * id string, this component joins those ids against the *same query's*
 * `QueryOutcome.payloads.evidence_panel` rows (agents/synthesis.py's `EvidenceRow`,
 * which carries the identical `provenance_id` key for exactly this purpose) — passed
 * down as the `evidencePanel` prop by ConsoleApp, the only place in this tree a
 * freshly-answered `QueryOutcome` exists in state (via AnalystQuery's
 * `onQueryComplete`).
 *
 * That evidence is only available for queries answered *this session*: the trace store
 * persists `TraceStep`s but never a query's evidence panel, and old queries are not
 * re-answerable (no endpoint returns a stored `QueryOutcome`). For a trace selected from
 * the "Recent queries" list that wasn't just answered in this tab, `evidencePanel` is
 * empty and each provenance id falls back to a partial render — the source id and
 * timestamp parsed straight out of the id string — with a note that the full record
 * isn't available. Closing that gap for real needs a backend surface this file's scope
 * didn't include changing (e.g. persisting evidence_panel rows alongside TraceStep in
 * the trace store, or a `GET /api/query/{query_id}` outcome-replay endpoint).
 */
import { useEffect, useMemo, useState } from "react";
import { getTrace } from "@shared/api";
import type { EvidencePanelRow, TraceTreeNode } from "@shared/types";
import { formatClock, formatDuration, formatTimeAgo, freshnessVar, shortId } from "./format";
import type { TraceListRow } from "./useConsoleData";

/** Recovers the two halves of a provenance id string when no EvidencePanelRow is
 *  available to join against — the degraded-but-still-useful fallback render. */
function parseProvenanceId(id: string): { sourceId: string; timestamp: string | null } {
  const idx = id.lastIndexOf("@");
  if (idx === -1) return { sourceId: id, timestamp: null };
  return { sourceId: id.slice(0, idx), timestamp: id.slice(idx + 1) };
}

const KIND_VAR: Record<string, string> = {
  plan: "var(--accent)",
  tool_call: "var(--ink-500)",
  tool_result: "var(--verdict-go)",
  synthesis: "var(--accent-dim)",
  ceiling: "var(--verdict-caution)",
  error: "var(--verdict-stop)",
};

function formatArgs(args: Record<string, unknown>): string {
  const entries = Object.entries(args ?? {});
  if (entries.length === 0) return "(no args)";
  const parts = entries.map(([k, v]) => {
    const s = typeof v === "string" ? v : JSON.stringify(v);
    const trimmed = s.length > 40 ? `${s.slice(0, 40)}…` : s;
    return `${k}=${trimmed}`;
  });
  return parts.join(", ");
}

interface TraceInspectorProps {
  traces: TraceListRow[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  /** The selected query's own evidence_panel rows, when this session has them (see
   *  the module docstring for when that is / isn't the case). Used only to join
   *  against each step's provenance_ids — never rendered as its own table here, the
   *  analyst query tab already does that. */
  evidencePanel?: EvidencePanelRow[] | null;
}

export default function TraceInspector({ traces, selectedId, onSelect, evidencePanel }: TraceInspectorProps) {
  const [detail, setDetail] = useState<TraceTreeNode[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    getTrace(selectedId)
      .then((res) => {
        if (!cancelled) setDetail(res.steps);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const evidenceByProvenanceId = useMemo(() => {
    const map = new Map<string, EvidencePanelRow>();
    for (const row of evidencePanel ?? []) map.set(row.provenance_id, row);
    return map;
  }, [evidencePanel]);

  return (
    <div className="trace-inspector">
      <div className="trace-inspector__list">
        <div className="trace-inspector__list-heading">Recent queries</div>
        {traces.length === 0 && <p className="empty-note">No queries recorded yet.</p>}
        {traces.map((t) => (
          <button
            key={t.query_id}
            type="button"
            className={`trace-row${t.query_id === selectedId ? " trace-row--selected" : ""}`}
            onClick={() => onSelect(t.query_id)}
          >
            <span className="trace-row__id">{shortId(t.query_id)}</span>
            <span className="trace-row__time">{formatClock(t.started_at)}</span>
            <span className="trace-row__steps">{t.step_count} steps</span>
            <span className="trace-row__agents">{t.agents.join(", ")}</span>
          </button>
        ))}
      </div>
      <div className="trace-inspector__detail">
        {!selectedId && <p className="empty-note">Select a query to inspect its reasoning trace.</p>}
        {loading && <p className="empty-note">Loading trace…</p>}
        {error && <p className="empty-note empty-note--error">{error}</p>}
        {detail && detail.length === 0 && <p className="empty-note">Trace has no recorded steps.</p>}
        {detail && (
          <ol className="trace-timeline">
            {detail.map((node) => (
              <TraceStepView
                key={node.step.step_id}
                node={node}
                depth={0}
                evidenceByProvenanceId={evidenceByProvenanceId}
              />
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}

function TraceStepView({
  node,
  depth,
  evidenceByProvenanceId,
}: {
  node: TraceTreeNode;
  depth: number;
  evidenceByProvenanceId: Map<string, EvidencePanelRow>;
}) {
  const { step } = node;
  return (
    <li className="trace-step" style={{ marginLeft: depth * 16 }}>
      <div className="trace-step__row">
        <span className="badge" style={{ background: KIND_VAR[step.kind] ?? "var(--ink-500)" }}>
          {step.kind}
        </span>
        <span className="trace-step__agent">{step.agent}</span>
        {step.tool && <span className="trace-step__tool">{step.tool}</span>}
        <span className="trace-step__duration">{formatDuration(step.duration_ms)}</span>
        <span className="trace-step__ts">{formatClock(step.ts)}</span>
        {!step.ok && <span className="badge" style={{ background: "var(--verdict-stop)" }}>error</span>}
      </div>
      {step.why && <div className="trace-step__why">{step.why}</div>}
      <div className="trace-step__args">{formatArgs(step.args)}</div>
      {step.result_digest && <div className="trace-step__digest">{step.result_digest}</div>}
      {step.error && <div className="trace-step__error">{step.error}</div>}
      {step.provenance_ids.length > 0 && (
        <details className="trace-step__provenance">
          <summary>{step.provenance_ids.length} provenance record(s)</summary>
          <ul>
            {step.provenance_ids.map((id) => {
              const row = evidenceByProvenanceId.get(id);
              if (row) {
                return (
                  <li key={id} className="trace-step__provenance-row">
                    <span className="trace-step__provenance-source">
                      {row.source_name}
                      <span className="trace-step__provenance-authority"> ({row.authority})</span>
                    </span>
                    <span className="trace-step__provenance-value">
                      {row.variable}: {row.display}
                    </span>
                    <span className="trace-step__provenance-meta">
                      {row.resolution} · acquired {formatTimeAgo(row.acquired_at)}
                    </span>
                    <span className="badge" style={{ background: freshnessVar(row.freshness) }}>
                      {row.freshness}
                    </span>
                    {row.is_derived && <span className="badge badge--outline">derived</span>}
                  </li>
                );
              }
              const { sourceId, timestamp } = parseProvenanceId(id);
              return (
                <li key={id} className="trace-step__provenance-row trace-step__provenance-row--partial">
                  <span className="trace-step__provenance-source">{sourceId}</span>
                  <span className="trace-step__provenance-meta">
                    {timestamp ? formatTimeAgo(timestamp) : "—"}
                  </span>
                  <span className="trace-step__provenance-note">
                    full record unavailable — not answered this session
                  </span>
                </li>
              );
            })}
          </ul>
        </details>
      )}
      {node.children.length > 0 && (
        <ol className="trace-timeline trace-timeline--nested">
          {node.children.map((child) => (
            <TraceStepView
              key={child.step.step_id}
              node={child}
              depth={depth + 1}
              evidenceByProvenanceId={evidenceByProvenanceId}
            />
          ))}
        </ol>
      )}
    </li>
  );
}
