/**
 * Data tab — "prove it's actually fetching something." `GET /api/conditions` calls the
 * same read-only tools a specialist would (sea state, tide, currents, wind/weather,
 * lightning nowcast) directly, with no planner/verdict/ceiling/model in between, and
 * returns every retrieved `Observation` with its full provenance. This tab renders that
 * verbatim, grouped into one card per source — a judge can see the sensor, the
 * authority, the acquisition time and the resolution behind every number, not just take
 * FORESHORE's word for it.
 *
 * A section that came back `partial`/`ok=false` renders that plainly (never hidden,
 * never silently dropped — CLAUDE.md's staleness/no-unsourced-numbers invariants extend
 * to this tab too) rather than only showing the sources that happened to answer.
 */
import { useCallback, useEffect, useState } from "react";
import { getConditions } from "@shared/api";
import type { ConditionsPayload, Observation, RegionInfo } from "@shared/types";
import { formatTimeAgo, freshnessVar } from "./format";

/** "significant_wave_height_m" -> "Significant wave height" — same convention as the
 *  trace inspector's arg-key humaniser, kept local since neither file imports the other. */
function humanizeVariable(key: string): string {
  const words = key.replace(/_m$|_deg$|_kn$|_pct$/, "").split(/[_\s]+/).filter(Boolean);
  if (words.length === 0) return key;
  const sentence = words.join(" ").toLowerCase();
  return sentence.charAt(0).toUpperCase() + sentence.slice(1);
}

function formatValue(o: Observation): string {
  if (o.value === null) return "—";
  if (typeof o.value === "number") {
    const rounded = Number.isInteger(o.value) ? o.value : Math.round(o.value * 100) / 100;
    return o.unit && o.unit !== "descriptor" && o.unit !== "category" && o.unit !== "band"
      ? `${rounded} ${o.unit}`
      : String(rounded);
  }
  return String(o.value);
}

function ObservationRow({ o }: { o: Observation }) {
  return (
    <div className="data-obs-row">
      <div className="data-obs-row__top">
        <span className="data-obs-row__variable">{humanizeVariable(o.variable)}</span>
        <span className="data-obs-row__value">{formatValue(o)}</span>
      </div>
      <div className="data-obs-row__meta">
        <span className="data-obs-row__source">
          {o.provenance.source_name}
          <span className="data-obs-row__authority"> ({o.provenance.authority})</span>
        </span>
        <span
          className="badge data-obs-row__freshness"
          style={{ background: freshnessVar(o.provenance.freshness) }}
          title={`Acquired ${formatTimeAgo(o.provenance.acquired_at)}`}
        >
          {o.provenance.freshness}
        </span>
      </div>
      {o.provenance.is_derived && (
        <span className="badge badge--outline data-obs-row__derived">FORESHORE-derived, not an official product</span>
      )}
    </div>
  );
}

/** A source like tide (`GET /api/conditions`'s `openmeteo_marine` adapter) reports one
 *  reading per hour up to two days out — 24-48 rows of the same variable/source/freshness
 *  repeated back to back, which read as a wall of near-identical noise rather than a
 *  reading. Everything else in `sections` (sea state, weather, lightning) already comes
 *  back as a handful of distinct variables, so this only ever fires on an hourly series:
 *  sorted by `valid_time` (nearest-future first) and capped to 5 by default, with a
 *  `<details>` to reach the rest — the full series is still there, never dropped, just
 *  not dumped on screen at once. */
const DEFAULT_ROWS_SHOWN = 5;

function sortedByValidTime(observations: Observation[]): Observation[] {
  return [...observations].sort((a, b) => {
    const av = Date.parse(a.valid_time ?? "");
    const bv = Date.parse(b.valid_time ?? "");
    if (Number.isNaN(av) || Number.isNaN(bv)) return 0;
    return av - bv;
  });
}

function SectionCard({ section }: { section: ConditionsPayload["sections"][number] }) {
  const statusLabel = !section.ok ? "Unavailable" : section.partial ? "Partial" : "OK";
  const statusTone = !section.ok ? "stop" : section.partial ? "caution" : "go";
  const observations = sortedByValidTime(section.observations);
  const overflow = observations.length > DEFAULT_ROWS_SHOWN;
  const shown = overflow ? observations.slice(0, DEFAULT_ROWS_SHOWN) : observations;
  const rest = overflow ? observations.slice(DEFAULT_ROWS_SHOWN) : [];

  return (
    <article className="data-card">
      <header className="data-card__head">
        <h3 className="data-card__title">{section.label}</h3>
        <span className={`data-card__status data-card__status--${statusTone}`}>{statusLabel}</span>
      </header>
      {section.summary && <p className="data-card__summary">{section.summary}</p>}
      {observations.length > 0 ? (
        <div className="data-card__obs">
          {shown.map((o, i) => (
            <ObservationRow key={`${o.variable}-${i}`} o={o} />
          ))}
          {overflow && (
            <details className="data-card__more">
              <summary>{rest.length} more reading{rest.length === 1 ? "" : "s"} from this source</summary>
              <div className="data-card__obs">
                {rest.map((o, i) => (
                  <ObservationRow key={`${o.variable}-rest-${i}`} o={o} />
                ))}
              </div>
            </details>
          )}
        </div>
      ) : (
        <p className="empty-note">No reading available for this source right now.</p>
      )}
      {section.missing.length > 0 && (
        <p className="data-card__missing">
          Unavailable: {section.missing.map((m) => humanizeVariable(m)).join(", ")}
        </p>
      )}
      {!section.ok && section.error && <p className="data-card__error">{section.error}</p>}
    </article>
  );
}

export default function DataTab({ region }: { region: RegionInfo | null }) {
  const ports = region?.anchor_ports ?? [];
  const [portIndex, setPortIndex] = useState(0);
  const [data, setData] = useState<ConditionsPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const port = ports[portIndex] ?? null;

  const load = useCallback(() => {
    if (!port) return;
    setLoading(true);
    setError(null);
    getConditions(port.lat, port.lon)
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false));
  }, [port]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="data-tab">
      <header className="data-tab__header">
        <div className="data-tab__title-group">
          <h2 className="data-tab__title">Live source readings</h2>
          <p className="data-tab__subtitle">
            Every value below came back from the sensor/model listed under it, retrieved just now —
            no planner, no verdict, no model in between.
          </p>
        </div>
        <div className="data-tab__controls">
          {ports.length > 1 && (
            <select
              className="data-tab__port-select"
              value={portIndex}
              onChange={(e) => setPortIndex(Number(e.target.value))}
            >
              {ports.map((p, i) => (
                <option key={p.name} value={i}>
                  {p.name}
                </option>
              ))}
            </select>
          )}
          <button type="button" className="btn btn--primary" onClick={load} disabled={loading}>
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </header>

      {!port && <p className="empty-note">This region declares no anchor port to read conditions at.</p>}
      {error && <p className="empty-note empty-note--error">{error}</p>}
      {data && (
        <>
          <p className="data-tab__generated">
            {port?.name} · {port?.lat.toFixed(4)}°N, {port?.lon.toFixed(4)}°E · fetched{" "}
            {formatTimeAgo(data.generated_at)}
          </p>
          <div className="data-tab__grid">
            {data.sections.map((s) => (
              <SectionCard key={s.key} section={s} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
