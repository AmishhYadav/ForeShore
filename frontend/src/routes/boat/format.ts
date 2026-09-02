/**
 * Small, dependency-free display helpers for the boat UI. Nothing here invents a
 * number — every function either formats a value the backend already sent, or reads
 * one of two known field-name variants for the same value (see the note on
 * `readEvidenceDisplay`/`readEvidenceResolution` below) and picks whichever is present.
 *
 * Why the tolerant readers exist: `docs/API.md` / `shared/types.ts` describe
 * `evidence_panel` rows as `{value, unit, resolution_m, issued_at, ...}`, but the
 * running backend's `agents/synthesis.py::EvidenceRow.to_dict()` actually emits
 * `{display, resolution, ...}` (a pre-formatted string for the value, a pre-formatted
 * string for resolution, no separate `issued_at`). Both are read from the *same*
 * backend-supplied row — this is not client-side computation of a new number, it is
 * picking the field that is actually populated. Flagged for reconciliation in the
 * handoff report; do not "fix" by guessing which side is right.
 */
import type { Freshness, VerdictLevel } from "@shared/types";

export interface FreshnessMeta {
  label: string;
  fg: string;
  bg: string;
}

const FRESHNESS_META: Record<Freshness, FreshnessMeta> = {
  live: { label: "LIVE", fg: "var(--verdict-go)", bg: "var(--verdict-go-bg)" },
  recent: { label: "RECENT", fg: "var(--severity-advisory)", bg: "var(--ink-800)" },
  stale: { label: "STALE", fg: "var(--verdict-caution)", bg: "var(--verdict-caution-bg)" },
  expired: { label: "EXPIRED", fg: "var(--verdict-stop)", bg: "var(--verdict-stop-bg)" },
};

export function freshnessMeta(freshness: Freshness | string | null | undefined): FreshnessMeta {
  if (freshness && freshness in FRESHNESS_META) return FRESHNESS_META[freshness as Freshness];
  return { label: String(freshness ?? "UNKNOWN").toUpperCase(), fg: "var(--ink-100)", bg: "var(--ink-800)" };
}

export interface VerdictMeta {
  fg: string;
  bg: string;
}

const VERDICT_META: Record<VerdictLevel, VerdictMeta> = {
  GO: { fg: "var(--verdict-go)", bg: "var(--verdict-go-bg)" },
  GO_WITH_CAUTION: { fg: "var(--verdict-caution)", bg: "var(--verdict-caution-bg)" },
  DO_NOT_ADVISE: { fg: "var(--verdict-stop)", bg: "var(--verdict-stop-bg)" },
};

export function verdictMeta(level: VerdictLevel | string | null | undefined): VerdictMeta {
  if (level && level in VERDICT_META) return VERDICT_META[level as VerdictLevel];
  return { fg: "var(--ink-100)", bg: "var(--ink-800)" };
}

const ALERT_LEVEL_META: Record<string, VerdictMeta> = {
  INFO: { fg: "var(--severity-advisory)", bg: "var(--ink-800)" },
  WARN: { fg: "var(--verdict-caution)", bg: "var(--verdict-caution-bg)" },
  CRITICAL: { fg: "var(--verdict-stop)", bg: "var(--verdict-stop-bg)" },
  BREACH: { fg: "var(--verdict-stop)", bg: "var(--verdict-stop-bg)" },
};

export function alertLevelMeta(level: string | null | undefined): VerdictMeta {
  if (level && level in ALERT_LEVEL_META) return ALERT_LEVEL_META[level];
  return { fg: "var(--ink-100)", bg: "var(--ink-800)" };
}

const SEVERITY_COLOR: Record<string, string> = {
  legal_hard: "var(--severity-legal)",
  legal: "var(--severity-legal)",
  hazard: "var(--severity-hazard)",
  restricted: "var(--severity-restricted)",
  advisory: "var(--severity-advisory)",
};

export function severityColor(severity: string | null | undefined): string {
  if (severity && severity in SEVERITY_COLOR) return SEVERITY_COLOR[severity];
  return "var(--ink-500)";
}

export function formatDistanceNm(nm: number | null | undefined): string {
  if (nm === null || nm === undefined || Number.isNaN(nm)) return "unknown distance";
  return `${nm.toFixed(2)} nm`;
}

export function formatEtaSeconds(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds) || seconds <= 0) return null;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

export function formatClock(iso: string | null | undefined): string {
  if (!iso) return "unknown";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "unknown";
  return d.toLocaleString(undefined, { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "short" });
}

/** True backend row shape carries `display` (pre-formatted "1.8 m") instead of the
 * documented separate `value`/`unit`. Prefer whichever is actually present. */
export function readEvidenceDisplay(row: Record<string, unknown>): string {
  if (typeof row.display === "string" && row.display) return row.display;
  const value = row.value;
  const unit = typeof row.unit === "string" ? row.unit : "";
  if (value === null || value === undefined) return "—";
  return unit ? `${value} ${unit}`.trim() : String(value);
}

/** True backend row shape carries `resolution` (pre-formatted "11 km") instead of the
 * documented `resolution_m` number. Prefer whichever is actually present. */
export function readEvidenceResolution(row: Record<string, unknown>): string {
  if (typeof row.resolution === "string" && row.resolution) return row.resolution;
  const m = row.resolution_m;
  if (typeof m === "number") return m >= 1000 ? `${(m / 1000).toFixed(0)} km` : `${m.toFixed(0)} m`;
  return "point/text";
}

export function readEvidenceAcquiredAt(row: Record<string, unknown>): string | null {
  const v = row.acquired_at ?? row.issued_at;
  return typeof v === "string" ? v : null;
}

/** [lat, lon] (this codebase's convention everywhere outside raw GeoJSON geometry) ->
 * maplibre's [lng, lat]. */
export function toLngLat(pair: readonly [number, number]): [number, number] {
  return [pair[1], pair[0]];
}

/**
 * `region.basemap.center` is stored as [lat, lon] (matches this app's convention) while
 * `region.bbox` is [minLon, minLat, maxLon, maxLat] (GeoJSON convention) — the two
 * fields disagree on axis order within the same `/api/region` response. Verified
 * against both shipped region configs (palk_bay_gom, gujarat_sir_creek): in each,
 * center[0] falls inside the bbox's lat range and center[1] inside its lon range, never
 * the other way round. Sanity-check against the bbox rather than trusting blindly, and
 * fall back to the bbox midpoint if the shape is ever something else. Flagged in the
 * handoff report — this should be made consistent one way or the other upstream.
 */
export function basemapCenterLngLat(
  basemap: Record<string, unknown> | undefined,
  bbox: [number, number, number, number] | undefined,
): [number, number] {
  const center = basemap?.center;
  if (Array.isArray(center) && center.length === 2 && typeof center[0] === "number" && typeof center[1] === "number") {
    const [a, b] = center as [number, number];
    if (bbox) {
      const [minLon, minLat, maxLon, maxLat] = bbox;
      const aIsLat = a >= minLat && a <= maxLat;
      const bIsLon = b >= minLon && b <= maxLon;
      if (aIsLat && bIsLon) return [b, a]; // [lat, lon] -> [lon, lat]
      const aIsLon = a >= minLon && a <= maxLon;
      const bIsLat = b >= minLat && b <= maxLat;
      if (aIsLon && bIsLat) return [a, b]; // already [lon, lat]
    }
    // No bbox to check against — this codebase's convention is [lat, lon] everywhere
    // else, so assume that.
    return [b, a];
  }
  if (bbox) {
    const [minLon, minLat, maxLon, maxLat] = bbox;
    return [(minLon + maxLon) / 2, (minLat + maxLat) / 2];
  }
  return [79.2, 9.3]; // last resort: Palk Bay, never (0,0)
}
