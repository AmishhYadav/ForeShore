/**
 * Trace inspector — "are those real agents, or five boxes on a slide?"
 *
 * `GET /api/trace/{query_id}` (see backend/foreshore/store/traces.py::TraceStore.tree)
 * returns a *nested* tree keyed by `parent_id`, not the flat `TraceStep[]` that
 * `shared/types.ts`'s `TraceStep` interface implies (that interface also names the
 * field `parent`; the backend's `TraceStep.to_dict()` emits `parent_id` — a mismatch
 * worth fixing in shared/types.ts, noted rather than touched here per the brief). Local
 * types below mirror the verified runtime shape instead of the shared interface.
 */
import { useEffect, useState } from "react";
import { getTrace } from "@shared/api";
import { formatClock, formatDuration, shortId } from "./format";
import type { TraceListRow } from "./useConsoleData";

interface TraceStepRecord {
  step_id: string;
  query_id: string;
  parent_id: string | null;
  agent: string;
  kind: string;
  tool: string | null;
  args: Record<string, unknown>;
  result_digest: string;
  provenance_ids: string[];
  duration_ms: number;
  ts: string;
  why: string | null;
  ok: boolean;
  error: string | null;
}

interface TraceNode {
  step: TraceStepRecord;
  children: TraceNode[];
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
}

export default function TraceInspector({ traces, selectedId, onSelect }: TraceInspectorProps) {
  const [detail, setDetail] = useState<TraceNode[] | null>(null);
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
        if (!cancelled) setDetail(res.steps as unknown as TraceNode[]);
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
              <TraceStepView key={node.step.step_id} node={node} depth={0} />
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}

function TraceStepView({ node, depth }: { node: TraceNode; depth: number }) {
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
            {step.provenance_ids.map((id) => (
              <li key={id}>{id}</li>
            ))}
          </ul>
        </details>
      )}
      {node.children.length > 0 && (
        <ol className="trace-timeline trace-timeline--nested">
          {node.children.map((child) => (
            <TraceStepView key={child.step.step_id} node={child} depth={depth + 1} />
          ))}
        </ol>
      )}
    </li>
  );
}
