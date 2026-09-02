/**
 * Analyst query box — an English, typed console-side call into the exact same
 * `POST /api/query` the boat UI's voice path hits (`surface: "console"` is the only
 * difference), rendered with this surface's own verdict/evidence-panel shape rather
 * than importing anything from routes/boat.
 */
import { useState, type FormEvent } from "react";
import { ApiError, postQuery } from "@shared/api";
import type { QueryOutcome } from "@shared/types";
import {
  formatDistanceNm,
  formatDuration,
  formatTimeAgo,
  freshnessVar,
  shortId,
  verdictBgVar,
  verdictLabel,
  verdictVar,
} from "./format";

/**
 * shared/types.ts's `EvidencePanelRow` declares `value`/`unit`/`issued_at`/`resolution_m`
 * fields the backend never sends. The actual row — see
 * backend/foreshore/agents/synthesis.py's `EvidenceRow` dataclass, verified against a
 * live `/api/query` response in fixture mode — carries a single pre-formatted `display`
 * string (e.g. "0.23 nm") and a `resolution` string (e.g. "point/text" or "11 km"), and
 * no `issued_at` at all. This type documents the verified runtime shape; the render
 * below uses it instead of the shared interface's fields.
 */
interface RuntimeEvidenceRow {
  variable: string;
  display: string;
  source_name: string;
  authority: string;
  resolution: string;
  freshness: string;
  acquired_at: string;
  is_derived: boolean;
  governs: boolean;
}

interface AnalystQueryProps {
  onQueryComplete: (queryId: string) => void;
  onViewTrace: (queryId: string) => void;
}

export default function AnalystQuery({ onQueryComplete, onViewTrace }: AnalystQueryProps) {
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<QueryOutcome | null>(null);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || loading) return;
    setLoading(true);
    setError(null);
    try {
      const res = await postQuery({ text: trimmed, surface: "console", use_model: true });
      setOutcome(res);
      onQueryComplete(res.query_id);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(`API ${err.status}: ${JSON.stringify(err.body)}`);
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="analyst-query">
      <form className="query-form" onSubmit={submit}>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder='Ask an operational question — e.g. "Which vessels are closest to the IMBL right now?"'
          rows={3}
        />
        <div className="query-form__actions">
          <button type="submit" className="btn btn--primary" disabled={loading || !text.trim()}>
            {loading ? "Asking…" : "Ask"}
          </button>
        </div>
      </form>
      {error && <p className="empty-note empty-note--error">{error}</p>}
      {outcome && <QueryResult outcome={outcome} onViewTrace={onViewTrace} />}
    </div>
  );
}

function QueryResult({
  outcome,
  onViewTrace,
}: {
  outcome: QueryOutcome;
  onViewTrace: (id: string) => void;
}) {
  const verdict = outcome.verdict;
  return (
    <div className="query-result">
      <div className="query-result__header">
        <span>Answered in {formatDuration(outcome.duration_ms)}</span>
        <span>Language: {outcome.language}</span>
        <button type="button" className="btn btn--link" onClick={() => onViewTrace(outcome.query_id)}>
          View full trace ({shortId(outcome.query_id)})
        </button>
      </div>
      <p className="query-result__text">{outcome.text}</p>

      {verdict ? (
        <div
          className="verdict-card"
          style={{ background: verdictBgVar(verdict.level), borderColor: verdictVar(verdict.level) }}
        >
          <div className="verdict-card__level" style={{ color: verdictVar(verdict.level) }}>
            {verdictLabel(verdict.level)}
          </div>
          {verdict.ceiling_applied && (
            <div className="verdict-card__ceiling">
              Downgraded from {verdictLabel(verdict.downgraded_from)} by the advisory ceiling
              {verdict.ceiling_source ? ` (${verdict.ceiling_source.source_name})` : ""}.
            </div>
          )}
          {verdict.reasons.length > 0 && (
            <ul className="verdict-card__reasons">
              {verdict.reasons.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          )}
          {verdict.level === "DO_NOT_ADVISE" && verdict.handoff && (
            <div className="verdict-card__handoff">
              Handoff: {verdict.handoff.authority_name} ({verdict.handoff.authority_type}) —{" "}
              {verdict.handoff.contact}
              {verdict.handoff.distance_nm != null ? `, ${formatDistanceNm(verdict.handoff.distance_nm)}` : ""}
            </div>
          )}
        </div>
      ) : (
        <p className="empty-note">No verdict was evaluated for this query.</p>
      )}

      {outcome.unsourced_numbers.length > 0 && (
        <div className="query-result__warning">
          Synthesis stripped {outcome.unsourced_numbers.length} unsourced value(s):{" "}
          {outcome.unsourced_numbers.join(", ")}
        </div>
      )}

      {outcome.payloads.evidence_panel.length > 0 && (
        <div className="evidence-table-wrap">
          <table className="evidence-table">
            <thead>
              <tr>
                <th>Variable</th>
                <th>Value</th>
                <th>Resolution</th>
                <th>Source</th>
                <th>Acquired</th>
                <th>Freshness</th>
                <th>Governs</th>
              </tr>
            </thead>
            <tbody>
              {(outcome.payloads.evidence_panel as unknown as RuntimeEvidenceRow[]).map((row, i) => (
                <tr key={i}>
                  <td>
                    {row.variable}
                    {row.is_derived ? " (derived)" : ""}
                  </td>
                  <td>{row.display}</td>
                  <td>{row.resolution}</td>
                  <td>
                    {row.source_name} <span className="evidence-table__authority">({row.authority})</span>
                  </td>
                  <td>{formatTimeAgo(row.acquired_at)}</td>
                  <td>
                    <span className="badge" style={{ background: freshnessVar(row.freshness) }}>
                      {row.freshness}
                    </span>
                  </td>
                  <td>{row.governs ? "governs" : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="query-result__footer">
        {outcome.specialists_used.length > 0 && <div>Specialists: {outcome.specialists_used.join(", ")}</div>}
        {outcome.missing.length > 0 && (
          <div className="query-result__missing">Missing: {outcome.missing.join(", ")}</div>
        )}
      </div>
    </div>
  );
}
