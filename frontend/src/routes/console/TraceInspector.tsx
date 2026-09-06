/**
 * Trace inspector — "are those real agents, or five boxes on a slide?"
 *
 * `GET /api/trace/{query_id}` (see backend/foreshore/store/traces.py::TraceStore.tree)
 * returns a nested tree of `TraceTreeNode`s keyed by `parent_id`. The raw tree is a flat
 * bookkeeping structure, not a reading order: a `plan` root fans out into six
 * `tool_call`/`tool_result` pairs plus a `ceiling` step, every node repeats the same
 * `args`/`digest` across its call and its result, and every digest is a JSON-quoted,
 * `…(len=NNN)`-elided, 200-char-truncated string (`store/traces.py::digest`). None of
 * that changes here — this file only regroups and re-renders the same steps so a judge
 * or an operator can read the reasoning top to bottom instead of scrolling a flat dump.
 *
 * The pipeline, in order:
 *   1. `flattenTree` drops the tree nesting (it carries no information `parent_id`
 *      doesn't already — see the backend docstring) into a flat `TraceStep[]`.
 *   2. `buildSections` buckets those steps into six fixed phases — Plan, Evidence
 *      gathered, Verdict, Advisory ceiling, Synthesis, Errors — plus a trailing "Other
 *      steps" catch-all so an unanticipated `kind` is never silently dropped. Within
 *      Evidence/Verdict, a `tool_call` and its one matching `tool_result` child merge
 *      into a single `ToolCardData` (call supplies `why`/`args`, result supplies
 *      `duration_ms`/`result_digest`/`provenance_ids`); an unmatched call or result
 *      still renders on its own. A merged or unmerged tool step whose result is
 *      `ok === false` is *not* shown as ordinary evidence — the FORESHORE invariant is
 *      that `DO_NOT_ADVISE` is a designed outcome, `ok === false` is the error state,
 *      and the two must never look like each other — so it renders in Errors instead.
 *      `plan`/`ceiling`/`synthesis` keep their dedicated section regardless of `ok`:
 *      those are deterministic bookkeeping steps this orchestrator writes itself, not
 *      fallible calls, so relocating one on an `ok` flag would hide the very thing
 *      (a downgraded ceiling, a completed plan) the section exists to surface.
 *   3. Every card ends with a collapsed "Raw step" `<details>` dumping the underlying
 *      `TraceStep`(s) verbatim — nothing the trace carries becomes unreachable, it is
 *      just no longer the default view.
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
 *
 * The six list-row fields (`question`, `surface`, `verdict`, `duration_ms`, `tool_ms`,
 * `ok`) are a newer addition to `GET /api/traces` and are read defensively throughout —
 * an older stored row, or a backend that hasn't restarted, can still omit them, and
 * every read here degrades to a neutral render rather than throwing.
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { getTrace } from "@shared/api";
import type { EvidencePanelRow, TraceStep, TraceTreeNode, VerdictLevel } from "@shared/types";
import {
  formatClock,
  formatDuration,
  formatTimeAgo,
  freshnessVar,
  shortId,
  verdictBgVar,
  verdictLabel,
  verdictVar,
} from "./format";
import type { TraceListRow } from "./useConsoleData";

/* ── small pure helpers — local to this file, not shared elsewhere ──────────────── */

/** Recovers the two halves of a provenance id string when no EvidencePanelRow is
 *  available to join against — the degraded-but-still-useful fallback render. */
function parseProvenanceId(id: string): { sourceId: string; timestamp: string | null } {
  const idx = id.lastIndexOf("@");
  if (idx === -1) return { sourceId: id, timestamp: null };
  return { sourceId: id.slice(0, idx), timestamp: id.slice(idx + 1) };
}

const VERDICT_LEVELS = new Set<VerdictLevel>(["GO", "GO_WITH_CAUTION", "DO_NOT_ADVISE"]);

/** Narrows an arbitrary string to `VerdictLevel` only when it is exactly one of the
 *  three canonical levels — never guesses, never accepts a fourth value. */
function asVerdictLevel(v: string | null | undefined): VerdictLevel | null {
  return typeof v === "string" && VERDICT_LEVELS.has(v as VerdictLevel) ? (v as VerdictLevel) : null;
}

/** Cleans a `store/traces.py::digest` string for reading:
 *  - a plain-string observation arrives JSON-quoted (`"…"`) — parse it back to the
 *    plain sentence when the quoting is intact;
 *  - `…(len=NNN)` (a string elided mid-value) becomes a plain `…`;
 *  - `…(+N more)` (a list elided past 6 items) becomes `… (+N more)`.
 *  A plan or ceiling digest is a JSON object/array truncated at a hard 200-char cap and
 *  will usually not round-trip through JSON.parse — that's expected, not an error; the
 *  guarded parse just leaves the truncated text as-is for the marker cleanup to still
 *  improve. Never attempts to pretty-print anything: a cut mid-JSON is unparsable by
 *  design and pretty-printing it would misrepresent the truncation as real structure. */
function cleanDigest(raw: string | null | undefined): string {
  if (!raw) return "";
  let text = raw.trim();
  if (text.length >= 2 && text.startsWith('"') && text.endsWith('"')) {
    try {
      const parsed: unknown = JSON.parse(text);
      if (typeof parsed === "string") text = parsed;
    } catch {
      // Truncated mid-string, not valid JSON — leave the quoted text as-is; the
      // marker cleanup below still helps even on the raw quoted form.
    }
  }
  return text.replace(/…\(len=\d+\)/g, "…").replace(/…\(\+(\d+) more\)/g, "… (+$1 more)");
}

const ISO_TS_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/;

function looksLikeIsoTimestamp(v: unknown): v is string {
  return typeof v === "string" && ISO_TS_RE.test(v) && !Number.isNaN(new Date(v).getTime());
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** `9.2876°N, 79.3129°E` — negative lat/lon flip the hemisphere letter rather than
 *  carrying a sign, which is how every other FORESHORE surface renders a position. */
function formatPosition(lat: unknown, lon: unknown): string | null {
  if (typeof lat !== "number" || typeof lon !== "number" || Number.isNaN(lat) || Number.isNaN(lon)) {
    return null;
  }
  const ns = lat < 0 ? "S" : "N";
  const ew = lon < 0 ? "W" : "E";
  return `${Math.abs(lat).toFixed(4)}°${ns}, ${Math.abs(lon).toFixed(4)}°${ew}`;
}

/** "vessel_class" -> "Vessel class", "evidence_query_id" -> "Evidence query id" —
 *  sentence case, only the first letter capitalised. */
function humanizeArgKey(key: string): string {
  const words = key.split(/[_\s]+/).filter(Boolean);
  if (words.length === 0) return key;
  const sentence = words.join(" ").toLowerCase();
  return sentence.charAt(0).toUpperCase() + sentence.slice(1);
}

const RULE_LABELS: Record<string, string> = {
  bulletin_expired: "IMD bulletin expired",
  douglas_band_cap: "Douglas band cap",
  port_signal: "Port signal hoisted",
  long_period_swell: "Long-period swell",
  missing_input: "Required input missing",
};

/** Known ceiling rule ids get a hand-written label; anything unmapped (a rule added
 *  later) still renders as words instead of a raw identifier — Title Case per word. */
function humanizeRuleId(id: string): string {
  const known = RULE_LABELS[id];
  if (known) return known;
  return id
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

interface ArgRow {
  key: string;
  label: string;
  value: string;
  title?: string;
}

/** Turns a step's `args` into display rows: `lat`/`lon` merge into one `Position` row,
 *  null/undefined values are dropped as noise, ISO timestamps render as a clock time
 *  with the raw ISO string as the `title`, arrays comma-join, objects compact-JSON with
 *  the full value in `title`, and every key is humanised. */
function buildArgRows(args: Record<string, unknown> | null | undefined): ArgRow[] {
  if (!args) return [];
  const rows: ArgRow[] = [];
  if (args.lat != null || args.lon != null) {
    const pos = formatPosition(args.lat, args.lon);
    if (pos) rows.push({ key: "__position", label: "Position", value: pos });
  }
  for (const [key, value] of Object.entries(args)) {
    if (key === "lat" || key === "lon") continue;
    if (value === null || value === undefined) continue;
    if (looksLikeIsoTimestamp(value)) {
      rows.push({ key, label: humanizeArgKey(key), value: formatClock(value), title: value });
      continue;
    }
    if (Array.isArray(value)) {
      const joined = value.map((v) => (typeof v === "string" ? v : JSON.stringify(v))).join(", ");
      rows.push({ key, label: humanizeArgKey(key), value: joined.length > 0 ? joined : "(empty)" });
      continue;
    }
    if (isPlainObject(value)) {
      const compact = JSON.stringify(value);
      const shown = compact.length > 60 ? `${compact.slice(0, 60)}…` : compact;
      rows.push({ key, label: humanizeArgKey(key), value: shown, title: compact });
      continue;
    }
    rows.push({ key, label: humanizeArgKey(key), value: String(value) });
  }
  return rows;
}

function joinMeta(parts: Array<string | null | undefined>): string {
  return parts.filter((p): p is string => Boolean(p)).join(" · ");
}

/* ── flattening + phase bucketing ────────────────────────────────────────────────── */

function flattenTree(nodes: TraceTreeNode[]): TraceStep[] {
  const out: TraceStep[] = [];
  const walk = (list: TraceTreeNode[]) => {
    for (const n of list) {
      out.push(n.step);
      if (n.children.length > 0) walk(n.children);
    }
  };
  walk(nodes);
  return out;
}

/** A `tool_call`/`tool_result` pair (or an unpaired one of either) collapsed into one
 *  card's worth of data — `why`/`args` from the call, `duration_ms`/`result_digest`/
 *  `provenance_ids` from the result, per the brief's merge rule. */
interface ToolCardData {
  key: string;
  tool: string;
  agent: string;
  why: string | null;
  args: Record<string, unknown>;
  durationMs: number | null;
  digest: string;
  provenanceIds: string[];
  ok: boolean;
  error: string | null;
  ts: string;
  noResult: boolean;
  noCall: boolean;
  rawSteps: TraceStep[];
}

type ErrorEntry =
  | { ts: string; render: "tool"; tool: ToolCardData }
  | { ts: string; render: "step"; step: TraceStep };

interface TraceSections {
  plan: TraceStep[];
  evidence: ToolCardData[];
  verdict: ToolCardData[];
  ceiling: TraceStep[];
  synthesis: TraceStep[];
  errors: ErrorEntry[];
  other: TraceStep[];
  /** Number of tool cards the plan produced, across Evidence + Verdict + any that
   *  failed into Errors — the Plan card's own summary line ("Planned N specialist tool
   *  calls") reads this rather than re-deriving it. */
  toolCallCount: number;
}

const VERDICT_TOOL = "evaluate_verdict";

function byTs<T extends { ts: string }>(a: T, b: T): number {
  return a.ts.localeCompare(b.ts);
}

function buildSections(steps: TraceStep[]): TraceSections {
  const childrenByParent = new Map<string, TraceStep[]>();
  for (const s of steps) {
    if (!s.parent_id) continue;
    const list = childrenByParent.get(s.parent_id);
    if (list) list.push(s);
    else childrenByParent.set(s.parent_id, [s]);
  }

  const consumed = new Set<string>();
  const toolCards: ToolCardData[] = [];

  // Pass 1 — merge a tool_call with its single matching tool_result child.
  for (const s of steps) {
    if (s.kind !== "tool_call") continue;
    const children = (childrenByParent.get(s.step_id) ?? []).filter(
      (c) => c.kind === "tool_result" && c.tool === s.tool,
    );
    if (children.length === 1) {
      const result = children[0];
      toolCards.push({
        key: s.step_id,
        tool: s.tool ?? "unknown_tool",
        agent: s.agent,
        why: s.why,
        args: s.args ?? {},
        durationMs: result.duration_ms,
        digest: result.result_digest,
        provenanceIds: result.provenance_ids,
        ok: result.ok,
        error: result.error,
        ts: s.ts,
        noResult: false,
        noCall: false,
        rawSteps: [s, result],
      });
      consumed.add(s.step_id);
      consumed.add(result.step_id);
    }
  }

  // Pass 2 — a tool_call left unconsumed had no (single) matching result.
  for (const s of steps) {
    if (s.kind !== "tool_call" || consumed.has(s.step_id)) continue;
    toolCards.push({
      key: s.step_id,
      tool: s.tool ?? "unknown_tool",
      agent: s.agent,
      why: s.why,
      args: s.args ?? {},
      durationMs: s.duration_ms,
      digest: "",
      provenanceIds: [],
      ok: s.ok,
      error: s.error,
      ts: s.ts,
      noResult: true,
      noCall: false,
      rawSteps: [s],
    });
    consumed.add(s.step_id);
  }

  // Pass 3 — a tool_result left unconsumed had no matching call.
  for (const s of steps) {
    if (s.kind !== "tool_result" || consumed.has(s.step_id)) continue;
    toolCards.push({
      key: s.step_id,
      tool: s.tool ?? "unknown_tool",
      agent: s.agent,
      why: s.why,
      args: s.args ?? {},
      durationMs: s.duration_ms,
      digest: s.result_digest,
      provenanceIds: s.provenance_ids,
      ok: s.ok,
      error: s.error,
      ts: s.ts,
      noResult: false,
      noCall: true,
      rawSteps: [s],
    });
    consumed.add(s.step_id);
  }

  const plan: TraceStep[] = [];
  const ceiling: TraceStep[] = [];
  const synthesis: TraceStep[] = [];
  const errorSteps: TraceStep[] = [];
  const other: TraceStep[] = [];

  for (const s of steps) {
    if (s.kind === "tool_call" || s.kind === "tool_result") continue; // handled above
    if (s.kind === "plan") plan.push(s);
    else if (s.kind === "ceiling") ceiling.push(s);
    else if (s.kind === "synthesis") synthesis.push(s);
    else if (s.kind === "error") errorSteps.push(s);
    else other.push(s);
  }

  const evidence: ToolCardData[] = [];
  const verdict: ToolCardData[] = [];
  const failedToolCards: ToolCardData[] = [];
  for (const card of toolCards) {
    // ok === false is FORESHORE's error state, kept visually distinct from a
    // DO_NOT_ADVISE verdict (a designed outcome, not a failure) — see module docstring.
    if (!card.ok) failedToolCards.push(card);
    else if (card.tool === VERDICT_TOOL) verdict.push(card);
    else evidence.push(card);
  }

  const errors: ErrorEntry[] = [
    ...errorSteps.map((step): ErrorEntry => ({ ts: step.ts, render: "step", step })),
    ...failedToolCards.map((tool): ErrorEntry => ({ ts: tool.ts, render: "tool", tool })),
  ];

  return {
    plan: plan.sort(byTs),
    evidence: evidence.sort(byTs),
    verdict: verdict.sort(byTs),
    ceiling: ceiling.sort(byTs),
    synthesis: synthesis.sort(byTs),
    errors: errors.sort(byTs),
    other: other.sort(byTs),
    // Every tool card the plan produced, failures included — a call that errored was
    // still planned, and undercounting it here would misreport the plan's own size.
    toolCallCount: toolCards.length,
  };
}

/** Wall-clock span across every loaded step, used only when the selected row's own
 *  `duration_ms` is absent (an older stored row) — the one place this file derives a
 *  count from the tree rather than reading it straight off the `TraceListRow`. */
function computeSpanMs(steps: TraceStep[]): number | null {
  if (steps.length < 2) return null;
  let min = steps[0].ts;
  let max = steps[0].ts;
  for (const s of steps) {
    if (s.ts < min) min = s.ts;
    if (s.ts > max) max = s.ts;
  }
  const diff = new Date(max).getTime() - new Date(min).getTime();
  return Number.isNaN(diff) ? null : diff;
}

/* ── presentational pieces ───────────────────────────────────────────────────────── */

function VerdictPill({ level, small }: { level: VerdictLevel | null; small?: boolean }) {
  return (
    <span
      className={`trace-verdict-pill${small ? " trace-verdict-pill--sm" : ""}`}
      style={{ color: verdictVar(level), background: verdictBgVar(level) }}
    >
      {verdictLabel(level)}
    </span>
  );
}

function RawStepDetails({ data }: { data: TraceStep | TraceStep[] }) {
  return (
    <details className="trace-card__raw">
      <summary>Raw step</summary>
      <pre>{JSON.stringify(data, null, 2)}</pre>
    </details>
  );
}

function ArgTable({ rows }: { rows: ArgRow[] }) {
  if (rows.length === 0) return null;
  return (
    <dl className="trace-card__kv">
      {rows.map((r) => (
        <div className="trace-card__kv-row" key={r.key}>
          <dt title={r.title}>{r.label}</dt>
          <dd title={r.title}>{r.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function ProvenanceList({
  ids,
  evidenceByProvenanceId,
}: {
  ids: string[];
  evidenceByProvenanceId: Map<string, EvidencePanelRow>;
}) {
  return (
    <ul className="trace-provenance-list">
      {ids.map((id) => {
        const row = evidenceByProvenanceId.get(id);
        if (row) {
          return (
            <li key={id} className="trace-provenance-row">
              <span className="trace-provenance-row__source">
                {row.source_name}
                <span className="trace-provenance-row__authority"> ({row.authority})</span>
              </span>
              <span className="trace-provenance-row__value">
                {row.variable}: {row.display}
              </span>
              <span className="trace-provenance-row__meta">
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
          <li key={id} className="trace-provenance-row trace-provenance-row--partial">
            <span className="trace-provenance-row__source">{sourceId}</span>
            <span className="trace-provenance-row__meta">{timestamp ? formatTimeAgo(timestamp) : "—"}</span>
            <span className="trace-provenance-row__note">full record unavailable — not answered this session</span>
          </li>
        );
      })}
    </ul>
  );
}

/** The tool card (#3 in the brief): name/agent/duration header, `why` as prose, the
 *  cleaned digest as body, then `Inputs`/`N sources` as two collapsed details on one
 *  row, then the raw-step escape hatch. Used for Evidence, Verdict, and any failed
 *  tool_call/tool_result pair that landed in Errors instead. */
function ToolCard({
  card,
  evidenceByProvenanceId,
}: {
  card: ToolCardData;
  evidenceByProvenanceId: Map<string, EvidencePanelRow>;
}) {
  const cleaned = cleanDigest(card.digest);
  const argRows = buildArgRows(card.args);
  return (
    <article className={`trace-card${card.ok ? "" : " trace-card--error"}`}>
      <header className="trace-card__head">
        <span className="trace-card__tool">{card.tool}</span>
        <span className="trace-card__agent">{card.agent}</span>
        <span className="trace-card__duration">{formatDuration(card.durationMs)}</span>
      </header>
      {card.why && <p className="trace-card__why">{card.why}</p>}
      {card.noResult && <p className="trace-card__note">No result recorded.</p>}
      {card.noCall && <p className="trace-card__note">No matching call recorded.</p>}
      {!card.ok && card.error && <p className="trace-card__error">{card.error}</p>}
      {cleaned && <p className="trace-card__digest">{cleaned}</p>}
      <div className="trace-card__aux-row">
        {argRows.length > 0 && (
          <details className="trace-card__aux">
            <summary>Inputs</summary>
            <ArgTable rows={argRows} />
          </details>
        )}
        {card.provenanceIds.length > 0 && (
          <details className="trace-card__aux">
            <summary>{card.provenanceIds.length} source{card.provenanceIds.length === 1 ? "" : "s"}</summary>
            <ProvenanceList ids={card.provenanceIds} evidenceByProvenanceId={evidenceByProvenanceId} />
          </details>
        )}
      </div>
      <RawStepDetails data={card.rawSteps.length === 1 ? card.rawSteps[0] : card.rawSteps} />
    </article>
  );
}

/** The plan card (#5): the plan's inputs — question, intents, position, when — rather
 *  than its truncated digest, plus a one-line summary of how many tool calls it
 *  produced. The truncated digest is reachable only via the raw-step toggle. */
function PlanCard({ step, plannedCount }: { step: TraceStep; plannedCount: number }) {
  const args = step.args ?? {};
  const text = typeof args.text === "string" && args.text ? args.text : null;
  const intents = Array.isArray(args.intents)
    ? args.intents.filter((i): i is string => typeof i === "string")
    : [];
  const position = formatPosition(args.lat, args.lon);
  const when = typeof args.when === "string" ? args.when : null;
  return (
    <article className="trace-card">
      <header className="trace-card__head">
        <span className="trace-card__tool">Plan</span>
        <span className="trace-card__agent">{step.agent}</span>
        <span className="trace-card__duration">{formatDuration(step.duration_ms)}</span>
      </header>
      {step.why && <p className="trace-card__why">{step.why}</p>}
      {text && <p className="trace-card__plan-question">“{text}”</p>}
      {intents.length > 0 && (
        <div className="trace-card__chips">
          {intents.map((intent) => (
            <span className="trace-chip" key={intent}>
              {intent}
            </span>
          ))}
        </div>
      )}
      {(position || when) && (
        <dl className="trace-card__kv">
          {position && (
            <div className="trace-card__kv-row">
              <dt>Position</dt>
              <dd>{position}</dd>
            </div>
          )}
          {when && (
            <div className="trace-card__kv-row">
              <dt>When</dt>
              <dd title={when}>{formatClock(when)}</dd>
            </div>
          )}
        </dl>
      )}
      <p className="trace-card__plan-summary">
        Planned {plannedCount} specialist tool call{plannedCount === 1 ? "" : "s"}
      </p>
      <RawStepDetails data={step} />
    </article>
  );
}

/** The ceiling card (#6): the verdict level (or the downgrade, made visually the point
 *  of the card — FORESHORE's central safety invariant), rules fired as humanised
 *  chips, then the cleaned digest as supporting text. Driven off `args`, which the
 *  orchestrator always writes complete, never off the truncated digest. */
function CeilingCard({ step }: { step: TraceStep }) {
  const args = step.args ?? {};
  const level = asVerdictLevel(typeof args.level === "string" ? args.level : null);
  const downgradedFrom = asVerdictLevel(
    typeof args.downgraded_from === "string" ? args.downgraded_from : null,
  );
  const rules = Array.isArray(args.rules_fired)
    ? args.rules_fired.filter((r): r is string => typeof r === "string")
    : [];
  const cleaned = cleanDigest(step.result_digest);
  return (
    <article className="trace-card">
      <header className="trace-card__head">
        <span className="trace-card__tool">Advisory ceiling</span>
        <span className="trace-card__agent">{step.agent}</span>
        <span className="trace-card__duration">{formatDuration(step.duration_ms)}</span>
      </header>
      {step.why && <p className="trace-card__why">{step.why}</p>}
      <div className="trace-ceiling__levels">
        {downgradedFrom ? (
          <>
            <VerdictPill level={downgradedFrom} />
            <span className="trace-ceiling__arrow" aria-hidden="true">
              →
            </span>
            <VerdictPill level={level} />
          </>
        ) : (
          <VerdictPill level={level} />
        )}
      </div>
      {rules.length > 0 && (
        <div className="trace-card__chips">
          {rules.map((r) => (
            <span className="trace-chip trace-chip--rule" key={r}>
              {humanizeRuleId(r)}
            </span>
          ))}
        </div>
      )}
      {cleaned && <p className="trace-card__digest">{cleaned}</p>}
      <RawStepDetails data={step} />
    </article>
  );
}

/** Generic card for synthesis, `kind: "error"` and any unanticipated kind ("Other
 *  steps") — agent/tool/duration header, why, error text, cleaned digest, inputs. */
function GenericStepCard({ step }: { step: TraceStep }) {
  const cleaned = cleanDigest(step.result_digest);
  const argRows = buildArgRows(step.args);
  return (
    <article className={`trace-card${step.ok ? "" : " trace-card--error"}`}>
      <header className="trace-card__head">
        <span className="trace-card__tool">{step.tool ?? step.kind}</span>
        <span className="trace-card__agent">{step.agent}</span>
        <span className="trace-card__duration">{formatDuration(step.duration_ms)}</span>
      </header>
      {step.why && <p className="trace-card__why">{step.why}</p>}
      {!step.ok && step.error && <p className="trace-card__error">{step.error}</p>}
      {cleaned && <p className="trace-card__digest">{cleaned}</p>}
      {argRows.length > 0 && (
        <div className="trace-card__aux-row">
          <details className="trace-card__aux">
            <summary>Inputs</summary>
            <ArgTable rows={argRows} />
          </details>
        </div>
      )}
      <RawStepDetails data={step} />
    </article>
  );
}

/* ── left rail ────────────────────────────────────────────────────────────────────── */

function TraceRow({
  row,
  selected,
  onSelect,
}: {
  row: TraceListRow;
  selected: boolean;
  onSelect: () => void;
}) {
  const verdict = asVerdictLevel(row.verdict);
  const tools = Array.isArray(row.tools) ? row.tools : [];
  const agents = Array.isArray(row.agents) ? row.agents : [];
  return (
    <button
      type="button"
      className={`trace-row${selected ? " trace-row--selected" : ""}`}
      onClick={onSelect}
    >
      <div className="trace-row__top">
        <VerdictPill level={verdict} small />
        <span className="trace-row__time" title={formatClock(row.started_at)}>
          {formatTimeAgo(row.started_at)}
        </span>
      </div>
      {row.question ? (
        <div className="trace-row__question">{row.question}</div>
      ) : (
        <div className="trace-row__question trace-row__question--fallback">
          Query {shortId(row.query_id)}
        </div>
      )}
      <div className="trace-row__meta">
        <span>{joinMeta([`${tools.length} tools`, `${agents.length} specialists`, formatDuration(row.duration_ms)])}</span>
        {row.surface && <span className="trace-row__surface">{row.surface}</span>}
        {row.ok === false && <span className="trace-row__fail">failed</span>}
      </div>
    </button>
  );
}

/* ── right pane ───────────────────────────────────────────────────────────────────── */

function TraceDetailHeader({ row, steps }: { row: TraceListRow; steps: TraceStep[] }) {
  const verdict = asVerdictLevel(row.verdict);
  const wallMs = typeof row.duration_ms === "number" ? row.duration_ms : computeSpanMs(steps);
  const stepCount = typeof row.step_count === "number" ? row.step_count : steps.length;
  const tools = Array.isArray(row.tools) ? row.tools : [];
  const agents = Array.isArray(row.agents) ? row.agents : [];
  return (
    <header className="trace-detail__header">
      <div className="trace-detail__top">
        <h2 className="trace-detail__question">
          {row.question || `Query ${shortId(row.query_id)}`}
        </h2>
        <VerdictPill level={verdict} />
      </div>
      <div className="trace-detail__meta">
        {joinMeta([
          formatClock(row.started_at),
          `${stepCount} steps`,
          `${tools.length} tools`,
          `${agents.length} specialists`,
          formatDuration(wallMs),
        ])}
      </div>
    </header>
  );
}

function PhaseSection({
  title,
  errorTone,
  children,
}: {
  title: string;
  errorTone?: boolean;
  children: ReactNode;
}) {
  return (
    <section className={`trace-phase${errorTone ? " trace-phase--errors" : ""}`}>
      <h3 className="trace-phase__title">{title}</h3>
      {children}
    </section>
  );
}

function TraceDetailBody({
  steps,
  evidenceByProvenanceId,
}: {
  steps: TraceStep[];
  evidenceByProvenanceId: Map<string, EvidencePanelRow>;
}) {
  const sections = useMemo(() => buildSections(steps), [steps]);
  return (
    <div className="trace-detail__body">
      {sections.plan.length > 0 && (
        <PhaseSection title="Plan">
          {sections.plan.map((s) => (
            <PlanCard key={s.step_id} step={s} plannedCount={sections.toolCallCount} />
          ))}
        </PhaseSection>
      )}
      {sections.evidence.length > 0 && (
        <PhaseSection title="Evidence gathered">
          {sections.evidence.map((c) => (
            <ToolCard key={c.key} card={c} evidenceByProvenanceId={evidenceByProvenanceId} />
          ))}
        </PhaseSection>
      )}
      {sections.verdict.length > 0 && (
        <PhaseSection title="Verdict">
          {sections.verdict.map((c) => (
            <ToolCard key={c.key} card={c} evidenceByProvenanceId={evidenceByProvenanceId} />
          ))}
        </PhaseSection>
      )}
      {sections.ceiling.length > 0 && (
        <PhaseSection title="Advisory ceiling">
          {sections.ceiling.map((s) => (
            <CeilingCard key={s.step_id} step={s} />
          ))}
        </PhaseSection>
      )}
      {sections.synthesis.length > 0 && (
        <PhaseSection title="Synthesis">
          {sections.synthesis.map((s) => (
            <GenericStepCard key={s.step_id} step={s} />
          ))}
        </PhaseSection>
      )}
      {sections.errors.length > 0 && (
        <PhaseSection title="Errors" errorTone>
          {sections.errors.map((e) =>
            e.render === "tool" ? (
              <ToolCard key={`tool-${e.tool.key}`} card={e.tool} evidenceByProvenanceId={evidenceByProvenanceId} />
            ) : (
              <GenericStepCard key={`step-${e.step.step_id}`} step={e.step} />
            ),
          )}
        </PhaseSection>
      )}
      {sections.other.length > 0 && (
        <PhaseSection title="Other steps">
          {sections.other.map((s) => (
            <GenericStepCard key={s.step_id} step={s} />
          ))}
        </PhaseSection>
      )}
    </div>
  );
}

/* ── top level ────────────────────────────────────────────────────────────────────── */

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

  const selectedRow = useMemo(
    () => traces.find((t) => t.query_id === selectedId) ?? null,
    [traces, selectedId],
  );

  const flatSteps = useMemo(() => (detail ? flattenTree(detail) : null), [detail]);

  return (
    <div className="trace-inspector">
      <div className="trace-inspector__list">
        <div className="trace-inspector__list-heading">Recent queries</div>
        {traces.length === 0 && <p className="empty-note">No queries recorded yet.</p>}
        {traces.map((t) => (
          <TraceRow key={t.query_id} row={t} selected={t.query_id === selectedId} onSelect={() => onSelect(t.query_id)} />
        ))}
      </div>
      <div className="trace-inspector__detail">
        {!selectedId && <p className="empty-note">Select a query to inspect its reasoning trace.</p>}
        {loading && <p className="empty-note">Loading trace…</p>}
        {error && <p className="empty-note empty-note--error">{error}</p>}
        {flatSteps && flatSteps.length === 0 && <p className="empty-note">Trace has no recorded steps.</p>}
        {flatSteps && flatSteps.length > 0 && selectedRow && (
          <>
            <TraceDetailHeader row={selectedRow} steps={flatSteps} />
            <TraceDetailBody steps={flatSteps} evidenceByProvenanceId={evidenceByProvenanceId} />
          </>
        )}
        {flatSteps && flatSteps.length > 0 && !selectedRow && selectedId && (
          // The tree loaded but the row that started this fetch has since scrolled out
          // of `traces` (a 15s poll trimmed the recent-queries list) — fall back to a
          // minimal row built from the id alone rather than losing the trace entirely.
          <>
            <TraceDetailHeader
              row={{
                query_id: selectedId,
                started_at: flatSteps[0]?.ts ?? "",
                agents: [],
                step_count: flatSteps.length,
                tools: [],
                question: null,
                surface: null,
                verdict: null,
                duration_ms: computeSpanMs(flatSteps) ?? 0,
                tool_ms: 0,
                ok: flatSteps.every((s) => s.ok),
              }}
              steps={flatSteps}
            />
            <TraceDetailBody steps={flatSteps} evidenceByProvenanceId={evidenceByProvenanceId} />
          </>
        )}
      </div>
    </div>
  );
}
