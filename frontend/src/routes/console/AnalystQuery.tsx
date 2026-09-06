/**
 * Analyst query box — an English, typed console-side call into the exact same
 * `POST /api/query` the boat UI's voice path hits (`surface: "console"` is the only
 * difference), rendered with this surface's own verdict/evidence-panel shape rather
 * than importing anything from routes/boat.
 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import { streamQuery } from "@shared/api";
import type { Handoff, QueryOutcome, Verdict } from "@shared/types";
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
  /** Fires with the full outcome (not just the id) so the parent can cache
   *  `payloads.evidence_panel` for the trace inspector's provenance join — see
   *  TraceInspector.tsx's module docstring. */
  onQueryComplete: (outcome: QueryOutcome) => void;
  onViewTrace: (queryId: string) => void;
}

export default function AnalystQuery({ onQueryComplete, onViewTrace }: AnalystQueryProps) {
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<QueryOutcome | null>(null);
  const [streamingText, setStreamingText] = useState("");
  const [phase, setPhase] = useState<{ phase: string; detail: string } | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // A new submit cancels whatever is still in flight, and unmounting mid-stream aborts
  // too — streamQuery's fetch is otherwise left running against a component that can no
  // longer render its result.
  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || loading) return;

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setLoading(true);
    setError(null);
    setStreamingText("");
    setPhase(null);

    await streamQuery(
      { text: trimmed, surface: "console", use_model: true },
      {
        onStatus: (s) => setPhase({ phase: s.phase, detail: s.detail }),
        onToken: (delta) => setStreamingText((prev) => prev + delta),
        onDone: (res) => {
          // done.text is authoritative — the draft is replaced wholesale, never merged
          // with what streamed (evidence audit / contract repairs / polish may all have
          // changed it since the last token).
          setStreamingText("");
          setPhase(null);
          setOutcome(res);
          onQueryComplete(res);
          setLoading(false);
        },
        onError: (message) => {
          setStreamingText("");
          setPhase(null);
          setError(message);
          setLoading(false);
        },
      },
      controller.signal,
    );
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
      {loading && <StreamingResult phase={phase} streamingText={streamingText} />}
      {!loading && outcome && <QueryResult outcome={outcome} onViewTrace={onViewTrace} />}
    </div>
  );
}

/**
 * Shown in place of the result header/text while a stream is in flight. Status events may
 * arrive with no tokens at all (template path — see streamQuery's doc comment), so this
 * renders correctly with an empty `streamingText` too: just the phase line and the empty
 * draft box, never a blank gap.
 */
function StreamingResult({
  phase,
  streamingText,
}: {
  phase: { phase: string; detail: string } | null;
  streamingText: string;
}) {
  return (
    <div className="query-result query-result--streaming">
      <div className="query-result__header">
        <span className="query-status">
          <span className="query-status__dot" />
          {phase?.detail ?? "Working…"}
        </span>
      </div>
      <p className="query-result__text query-result__text--streaming">
        {streamingText || <span className="query-result__text-waiting">Waiting for the draft…</span>}
      </p>
      <p className="query-result__draft-note">
        Draft — final answer is checked against the evidence before it is shown.
      </p>
    </div>
  );
}

/**
 * Two things about an answer that are invisible from the answer itself, and that were
 * costing real debugging time: whether the sources were live or a frozen snapshot, and
 * whether a model wrote the prose or the deterministic template did.
 *
 * Both fallbacks are correct by design — fixture mode exists so venue wifi cannot kill a
 * demo, and a template answer carries the same verdict, evidence and trace. But a
 * two-day-old fixture bulletin reads exactly like a genuine expiry, and template prose
 * reads like a terse model. Neither should have to be inferred from the response time.
 * Both chips render only when something is *not* the default, so a healthy answer stays
 * uncluttered.
 */
function ProvenanceChips({ outcome }: { outcome: QueryOutcome }) {
  const fixture = outcome.run_mode === "fixture";
  const model = outcome.payloads.model;
  const templated = model?.written_by === "template";
  if (!fixture && !templated) return null;
  return (
    <>
      {fixture && (
        <span className="chip chip--warn" title="Answers replay frozen snapshots from data/fixtures/">
          fixture data
        </span>
      )}
      {templated && (
        <span className="chip chip--warn" title={model?.degraded_reason ?? undefined}>
          template prose
        </span>
      )}
    </>
  );
}

/** The "why" behind those chips, in a sentence, under the answer. */
function AnswerProvenanceNote({ outcome }: { outcome: QueryOutcome }) {
  const model = outcome.payloads.model;
  const repairs = outcome.payloads.contract_repairs ?? [];
  const fixture = outcome.run_mode === "fixture";
  const templated = model?.written_by === "template";
  if (!fixture && !templated && repairs.length === 0) return null;
  return (
    <div className="query-result__provenance">
      {fixture && (
        <p>
          Sources are frozen snapshots from <code>data/fixtures/</code>, not today's data — every
          timestamp below is the snapshot's, so a bulletin may report itself expired when the live
          one is current. Unset <code>FORESHORE_MODE</code> for live sources.
        </p>
      )}
      {templated && (
        <p>
          The prose was composed from templates, not by a model
          {model?.degraded_reason ? `: ${model.degraded_reason}` : ""}. The verdict, evidence panel
          and trace are unaffected.
        </p>
      )}
      {repairs.length > 0 && (
        <p>
          Model output was repaired before display: {repairs.join("; ")}.
        </p>
      )}
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
  // Missing/undefined answer_kind defaults to ADVISORY (the safe default) — see the
  // field's doc comment on shared/types.ts's Plan interface.
  const answerKind = outcome.plan?.answer_kind ?? "ADVISORY";
  const evidenceRows = outcome.payloads.evidence_panel as unknown as RuntimeEvidenceRow[];

  return (
    <div className="query-result">
      <div className="query-result__header">
        <span>Answered in {formatDuration(outcome.duration_ms)}</span>
        <span>Language: {outcome.language}</span>
        <ProvenanceChips outcome={outcome} />
        <button type="button" className="btn btn--link" onClick={() => onViewTrace(outcome.query_id)}>
          View full trace ({shortId(outcome.query_id)})
        </button>
      </div>
      <p className="query-result__text">{outcome.text}</p>
      <AnswerProvenanceNote outcome={outcome} />

      {verdict ? (
        <VerdictBlock verdict={verdict} answerKind={answerKind} />
      ) : (
        <p className="empty-note">No verdict was evaluated for this query.</p>
      )}

      {outcome.unsourced_numbers.length > 0 && (
        <div className="query-result__warning">
          Synthesis stripped {outcome.unsourced_numbers.length} unsourced value(s):{" "}
          {outcome.unsourced_numbers.join(", ")}
        </div>
      )}

      {evidenceRows.length > 0 && <EvidenceTable rows={evidenceRows} />}

      <ResultFooter specialists={outcome.specialists_used} missing={outcome.missing} />
    </div>
  );
}

/**
 * The verdict is always computed, but it is only ever *the answer* on an ADVISORY
 * question (PLAN_V2's answer_kind gate). On an INFORMATIONAL question — "which vessels
 * are closest to the IMBL" — the same verdict is safety context for this position and
 * time, so it renders as a compact, clearly-labelled strip instead of the full card, and
 * never competes visually with `outcome.text` above it.
 *
 * Both presentations share the same reasons-disclosure and handoff sub-renders so the
 * duplication fix (reasons/handoff collapsed behind `<details>`, never repeated verbatim
 * against the prose) applies identically either way.
 */
function VerdictBlock({ verdict, answerKind }: { verdict: Verdict; answerKind: "ADVISORY" | "INFORMATIONAL" }) {
  const ceilingNote = verdict.ceiling_applied && (
    <div className="verdict-card__ceiling">
      Downgraded from {verdictLabel(verdict.downgraded_from)} by the advisory ceiling
      {verdict.ceiling_source ? ` (${verdict.ceiling_source.source_name})` : ""}.
    </div>
  );

  // The prose (`outcome.text`) already states the leading reason — this disclosure exists
  // so the full list is still reachable, not so it repeats what was just read above it.
  const reasonsDisclosure = verdict.reasons.length > 0 && (
    <details className="verdict-card__reasons-details">
      <summary>Why ({verdict.reasons.length} reasons)</summary>
      <ul className="verdict-card__reasons">
        {verdict.reasons.map((r, i) => (
          <li key={i}>{r}</li>
        ))}
      </ul>
    </details>
  );

  const handoff =
    verdict.level === "DO_NOT_ADVISE" && verdict.handoff ? <HandoffBlock handoff={verdict.handoff} /> : null;

  if (answerKind === "INFORMATIONAL") {
    return (
      <div className="verdict-strip" style={{ borderColor: verdictVar(verdict.level) }}>
        <div className="verdict-strip__head">
          <div className="verdict-strip__labels">
            <span className="verdict-strip__title">Sea-going advisory for this position and time</span>
            <span className="verdict-strip__note">Context — not the answer to your question</span>
          </div>
          <span
            className="verdict-strip__chip"
            style={{ color: verdictVar(verdict.level), background: verdictBgVar(verdict.level) }}
          >
            {verdictLabel(verdict.level)}
          </span>
        </div>
        {ceilingNote}
        {reasonsDisclosure}
        {handoff}
      </div>
    );
  }

  return (
    <div
      className="verdict-card"
      style={{ background: verdictBgVar(verdict.level), borderColor: verdictVar(verdict.level) }}
    >
      <div className="verdict-card__level" style={{ color: verdictVar(verdict.level) }}>
        {verdictLabel(verdict.level)}
      </div>
      {ceilingNote}
      {reasonsDisclosure}
      {handoff}
    </div>
  );
}

/**
 * Contact-safe handoff render (invariant 2 / CLAUDE.md's abstention path). Mirrors
 * backend/foreshore/agents/synthesis.py::template_answer's rule for `Handoff.contact` —
 * see the comment there and `contact_verified`'s doc comment on models.py's Handoff: a
 * demo-directory number (`contact_verified` false/undefined) must never render as a
 * `tel:` link or read as though it were published. Only Coast Guard 1554 is verified
 * today, and it always gets its own always-on `tel:` line regardless of what else is
 * known about the named authority.
 */
function HandoffBlock({ handoff }: { handoff: Handoff }) {
  const verifiedContact = handoff.contact_verified === true ? handoff.contact : null;
  const unverifiedContact = !verifiedContact && handoff.contact ? handoff.contact : null;
  const contactLabel = handoff.contact_label ?? "Contact";

  return (
    <div className="verdict-card__handoff">
      <div className="verdict-card__handoff-name">
        {handoff.authority_name}{" "}
        <span className="verdict-card__handoff-type">({handoff.authority_type.replace(/_/g, " ")})</span>
      </div>
      <dl className="verdict-card__handoff-meta">
        {handoff.distance_nm != null && (
          <div className="verdict-card__handoff-row">
            <dt>Distance</dt>
            <dd>{formatDistanceNm(handoff.distance_nm)}</dd>
          </div>
        )}
        {handoff.vhf_channel && (
          <div className="verdict-card__handoff-row">
            <dt>VHF</dt>
            <dd>{handoff.vhf_channel}</dd>
          </div>
        )}
        {verifiedContact && (
          <div className="verdict-card__handoff-row">
            <dt>{contactLabel}</dt>
            <dd>
              <a href={`tel:${verifiedContact}`}>{verifiedContact}</a>
            </dd>
          </div>
        )}
        {unverifiedContact && (
          <div className="verdict-card__handoff-row">
            <dt>{contactLabel}</dt>
            <dd>
              {unverifiedContact}{" "}
              <span className="verdict-card__handoff-unverified">
                (unverified demo directory number — confirm before dialling)
              </span>
            </dd>
          </div>
        )}
        <div className="verdict-card__handoff-row verdict-card__handoff-row--cg">
          <dt>Coast Guard</dt>
          <dd>
            <a href="tel:1554">1554</a>
          </dd>
        </div>
      </dl>
    </div>
  );
}

/** Governing rows first, then non-derived, then the rest — stable within each group (the
 * manual index carry rather than relying on Array.sort's own stability keeps this correct
 * regardless of engine). Collapsed into a single `<details>` so a several-dozen-row dump
 * doesn't bury the answer text above it; open by default only while it's still short
 * enough to be a glance rather than a scroll. */
function EvidenceTable({ rows }: { rows: RuntimeEvidenceRow[] }) {
  const rank = (r: RuntimeEvidenceRow) => (r.governs ? 0 : r.is_derived ? 2 : 1);
  const sorted = rows
    .map((row, index) => ({ row, index }))
    .sort((a, b) => rank(a.row) - rank(b.row) || a.index - b.index)
    .map(({ row }) => row);
  const governingCount = rows.filter((r) => r.governs).length;

  return (
    <details className="evidence-details" open={rows.length <= 8}>
      <summary>
        Evidence — {rows.length} sources ({governingCount} governing)
      </summary>
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
            {sorted.map((row, i) => (
              <tr key={i} className={row.governs ? "evidence-table__row--governs" : undefined}>
                <td>
                  {row.variable}
                  {row.is_derived && <span className="badge badge--outline">derived</span>}
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
                <td>{row.governs && <span className="badge badge--governs">governs</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function ResultFooter({ specialists, missing }: { specialists: string[]; missing: string[] }) {
  if (specialists.length === 0 && missing.length === 0) return null;
  return (
    <div className="query-result__footer">
      {specialists.length > 0 && (
        <div className="query-result__footer-row">
          <span className="query-result__footer-label">Specialists</span>
          <div className="chip-row">
            {specialists.map((s) => (
              <span className="trace-chip" key={s}>
                {s}
              </span>
            ))}
          </div>
        </div>
      )}
      {missing.length > 0 && (
        <div className="query-result__footer-row">
          <span className="query-result__footer-label">Unavailable:</span>
          <div className="chip-row">
            {missing.map((m) => (
              <span className="trace-chip trace-chip--rule" key={m}>
                {m}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
