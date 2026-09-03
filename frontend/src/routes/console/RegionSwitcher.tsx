/**
 * Region switcher — PLAN.md Phase 7 item 3 / the demo script's 6:30 closing beat.
 * Answers "does this only work for Tamil Nadu?" live: flips the backend's process-wide
 * active region (`POST /api/region/active`), re-fetches region info + geofences through
 * `useConsoleData.swapRegion`, and — to prove the swap is real rather than a relabelled
 * map — fires one live `POST /api/query` at the new region's own first anchor port and
 * shows the verdict + evidence it comes back with.
 *
 * The two known regions are hardcoded here rather than served by a "list regions"
 * endpoint (none exists — CLAUDE.md: region config only, keyless where possible, no new
 * backend surface for a two-item demo list). Source of truth for these two entries:
 *   - config/regions/palk_bay_gom.yaml       (display_name_en: "Palk Bay & Gulf of Mannar")
 *   - config/regions/gujarat_sir_creek.yaml  (display_name_en: "Sir Creek & Gulf of Kutch")
 * Add a third `{region_id, label}` pair here when a third yaml lands.
 */
import { useState } from "react";
import { ApiError, postQuery } from "@shared/api";
import type { QueryOutcome, RegionInfo } from "@shared/types";
import {
  formatDuration,
  freshnessVar,
  verdictBgVar,
  verdictLabel,
  verdictVar,
} from "./format";

/** See AnalystQuery.tsx's identical comment: the wire shape of
 * `QueryOutcome.payloads.evidence_panel` (verified live against a running backend) is a
 * flat pre-formatted `display` string plus `resolution`/`authority` as plain strings —
 * not shared/types.ts's `EvidencePanelRow` field names. Documented locally rather than
 * touching the shared, already-committed contract file. */
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

const KNOWN_REGIONS: { region_id: string; label: string }[] = [
  { region_id: "palk_bay_gom", label: "Palk Bay & Gulf of Mannar" },
  { region_id: "gujarat_sir_creek", label: "Sir Creek & Gulf of Kutch" },
];

const PROOF_QUESTION = "Is it safe to go out?";

type SwapStage = "idle" | "swapping" | "querying" | "done";

interface RegionSwitcherProps {
  currentRegion: RegionInfo | null;
  /** Bound to useConsoleData's swapRegion — flips the backend's active region and
   * refreshes the hook's own region + geofences state; returns the new RegionInfo so this
   * component can read its anchor_ports for the proof query without re-fetching. */
  onSwap: (regionId: string) => Promise<RegionInfo>;
}

export default function RegionSwitcher({ currentRegion, onSwap }: RegionSwitcherProps) {
  const [stage, setStage] = useState<SwapStage>("idle");
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [proof, setProof] = useState<QueryOutcome | null>(null);

  const currentId = currentRegion?.region_id ?? null;
  const busy = stage === "swapping" || stage === "querying";

  function describeError(err: unknown): string {
    if (err instanceof ApiError) return `API ${err.status}: ${JSON.stringify(err.body)}`;
    return err instanceof Error ? err.message : String(err);
  }

  async function handleSwap(regionId: string) {
    if (busy || regionId === currentId) return;
    setPendingId(regionId);
    setError(null);
    setProof(null);
    setStage("swapping");
    let newRegion: RegionInfo;
    try {
      newRegion = await onSwap(regionId);
    } catch (err) {
      setError(`Region swap failed: ${describeError(err)}`);
      setStage("idle");
      setPendingId(null);
      return;
    }

    const anchor = newRegion.anchor_ports[0];
    if (!anchor) {
      // Swap itself succeeded — only the proof step has nothing to query with.
      setError("Region swapped, but this region config has no anchor_ports to prove it with.");
      setStage("done");
      return;
    }

    setStage("querying");
    try {
      const outcome = await postQuery({
        text: PROOF_QUESTION,
        lat: anchor.lat,
        lon: anchor.lon,
        region_id: regionId,
        surface: "console",
      });
      setProof(outcome);
    } catch (err) {
      setError(`Region swapped to ${newRegion.display_name_en}, but the live proof query failed: ${describeError(err)}`);
    } finally {
      setStage("done");
    }
  }

  return (
    <div className="region-switcher">
      <p className="region-switcher__intro">
        Swaps the backend's live active region — boundary layers, advisory ceiling and language all
        re-home to the new config, proven with a real query below, not just a relabelled map.
      </p>

      <div className="region-switcher__options">
        {KNOWN_REGIONS.map((r) => {
          const isCurrent = r.region_id === currentId;
          return (
            <button
              key={r.region_id}
              type="button"
              className={`region-switcher__option${isCurrent ? " region-switcher__option--current" : ""}`}
              disabled={isCurrent || busy || currentRegion == null}
              onClick={() => handleSwap(r.region_id)}
            >
              <span className="region-switcher__option-label">{r.label}</span>
              <span className="region-switcher__option-status">
                {isCurrent
                  ? "Active now"
                  : busy && pendingId === r.region_id
                    ? stage === "swapping"
                      ? "Swapping…"
                      : "Querying live…"
                    : "Switch to this region"}
              </span>
            </button>
          );
        })}
      </div>

      {currentRegion == null && <p className="empty-note">Loading current region…</p>}

      {error && <p className="empty-note empty-note--error">{error}</p>}

      {(stage === "querying" || (busy && pendingId)) && !proof && (
        <p className="region-switcher__loading">
          Live query in flight for the new region's anchor port — this hits real upstream sources
          and can take a few seconds…
        </p>
      )}

      {proof && <ProofResult outcome={proof} />}

      <p className="region-switcher__limitation">
        Known limitation: the simulated vessel fleet was built once at backend startup for Palk
        Bay &amp; Gulf of Mannar and does not relocate on a region swap — the fleet map keeps
        showing Palk Bay boat positions regardless of the active region above.
      </p>
    </div>
  );
}

function ProofResult({ outcome }: { outcome: QueryOutcome }) {
  const verdict = outcome.verdict;
  const rows = outcome.payloads.evidence_panel as unknown as RuntimeEvidenceRow[];
  return (
    <div className="region-switcher__proof">
      <div className="region-switcher__proof-header">
        <span>Live proof query: &ldquo;{PROOF_QUESTION}&rdquo;</span>
        <span>Answered in {formatDuration(outcome.duration_ms)}</span>
      </div>

      {verdict ? (
        <div
          className="verdict-card"
          style={{ background: verdictBgVar(verdict.level), borderColor: verdictVar(verdict.level) }}
        >
          <div className="verdict-card__level" style={{ color: verdictVar(verdict.level) }}>
            {verdictLabel(verdict.level)}
          </div>
          {verdict.reasons.length > 0 && (
            <ul className="verdict-card__reasons">
              {verdict.reasons.slice(0, 2).map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <p className="empty-note">No verdict was evaluated for this query.</p>
      )}

      {rows.length > 0 && (
        <div className="evidence-table-wrap">
          <table className="evidence-table">
            <thead>
              <tr>
                <th>Variable</th>
                <th>Value</th>
                <th>Source</th>
                <th>Freshness</th>
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, 3).map((row, i) => (
                <tr key={i}>
                  <td>{row.variable}</td>
                  <td>{row.display}</td>
                  <td>
                    {row.source_name} <span className="evidence-table__authority">({row.authority})</span>
                  </td>
                  <td>
                    <span className="badge" style={{ background: freshnessVar(row.freshness) }}>
                      {row.freshness}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
