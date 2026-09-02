/**
 * Small, pure display helpers shared by the console's components. No network calls,
 * no state — just turning wire values (verdict levels, geofence classes, severities,
 * durations, timestamps) into the copy and CSS-token references the panels render.
 *
 * Colour lookups return `var(--token)` strings for use in plain DOM/CSS styling. MapLibre
 * paint properties run on canvas and cannot resolve CSS custom properties, so FleetMap.tsx
 * resolves the same tokens to literal hex via `getComputedStyle` at mount time instead of
 * duplicating a colour table here.
 */
import type { AlertLevel, Freshness, GeofenceClass, VerdictLevel } from "@shared/types";

export type Severity = "legal_hard" | "hazard" | "restricted" | "advisory";

/** Mirrors backend/foreshore/geofence/classes.py's LAYER_CLASS -> ClassSpec.severity
 *  wiring (config/geofence.yaml) — kept here as a display-only mirror since the console
 *  needs a class->severity mapping before any single feature has loaded (e.g. legend). */
export function severityForClass(gc: GeofenceClass | null | undefined): Severity {
  switch (gc) {
    case "IMBL_HISTORIC_WATERS":
    case "IMBL_MARITIME_BOUNDARY":
      return "legal_hard";
    case "MPA":
      return "restricted";
    case "ECO_SENSITIVE":
      return "advisory";
    case "HAZARD_EXCLUSION":
      return "hazard";
    case "USER_DEFINED":
      return "advisory";
    default:
      return "advisory";
  }
}

export function severityVar(severity: Severity | string | null | undefined): string {
  switch (severity) {
    case "legal_hard":
      return "var(--severity-legal)";
    case "hazard":
      return "var(--severity-hazard)";
    case "restricted":
      return "var(--severity-restricted)";
    case "advisory":
      return "var(--severity-advisory)";
    default:
      return "var(--ink-500)";
  }
}

export function geofenceClassLabel(gc: GeofenceClass | string | null | undefined): string {
  switch (gc) {
    case "IMBL_HISTORIC_WATERS":
      return "IMBL — historic waters (1974)";
    case "IMBL_MARITIME_BOUNDARY":
      return "IMBL — maritime boundary (1976)";
    case "MPA":
      return "Marine Protected Area";
    case "ECO_SENSITIVE":
      return "Eco-sensitive zone";
    case "USER_DEFINED":
      return "Operational boundary";
    case "HAZARD_EXCLUSION":
      return "Hazard exclusion";
    default:
      return gc ?? "Unknown";
  }
}

export function verdictVar(level: VerdictLevel | null | undefined): string {
  switch (level) {
    case "GO":
      return "var(--verdict-go)";
    case "GO_WITH_CAUTION":
      return "var(--verdict-caution)";
    case "DO_NOT_ADVISE":
      return "var(--verdict-stop)";
    default:
      return "var(--ink-500)";
  }
}

export function verdictBgVar(level: VerdictLevel | null | undefined): string {
  switch (level) {
    case "GO":
      return "var(--verdict-go-bg)";
    case "GO_WITH_CAUTION":
      return "var(--verdict-caution-bg)";
    case "DO_NOT_ADVISE":
      return "var(--verdict-stop-bg)";
    default:
      return "var(--ink-800)";
  }
}

export function verdictLabel(level: VerdictLevel | null | undefined): string {
  switch (level) {
    case "GO":
      return "GO";
    case "GO_WITH_CAUTION":
      return "GO WITH CAUTION";
    case "DO_NOT_ADVISE":
      return "DO NOT ADVISE";
    default:
      return "NO VERDICT";
  }
}

/** Backend's full AlertLevel is INFO/WARN/CRITICAL/BREACH (models.py); docs/API.md's
 *  "WARN/CRITICAL" is the common case but not exhaustive, so this stays permissive. */
export function alertLevelVar(level: AlertLevel | string | null | undefined): string {
  switch (level) {
    case "BREACH":
    case "CRITICAL":
      return "var(--verdict-stop)";
    case "WARN":
      return "var(--verdict-caution)";
    default:
      return "var(--ink-500)";
  }
}

export function freshnessVar(freshness: Freshness | string | null | undefined): string {
  switch (freshness) {
    case "live":
      return "var(--verdict-go)";
    case "recent":
      return "var(--verdict-caution)";
    case "stale":
    case "expired":
      return "var(--verdict-stop)";
    default:
      return "var(--ink-500)";
  }
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

export function formatDistanceNm(nm: number | null | undefined): string {
  if (nm == null || Number.isNaN(nm)) return "—";
  return `${nm.toFixed(2)} nm`;
}

export function formatEtaSeconds(s: number | null | undefined): string {
  if (s == null || Number.isNaN(s) || s <= 0) return "—";
  if (s < 60) return `${Math.round(s)} s`;
  const minutes = s / 60;
  if (minutes < 60) return `${minutes.toFixed(1)} min`;
  return `${(minutes / 60).toFixed(1)} h`;
}

export function formatTimeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diffS = Math.max(0, (Date.now() - then) / 1000);
  if (diffS < 5) return "just now";
  if (diffS < 60) return `${Math.round(diffS)}s ago`;
  if (diffS < 3600) return `${Math.round(diffS / 60)}m ago`;
  if (diffS < 86400) return `${Math.round(diffS / 3600)}h ago`;
  return `${Math.round(diffS / 86400)}d ago`;
}

export function formatClock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function shortId(id: string | null | undefined, len = 8): string {
  if (!id) return "—";
  return id.length > len ? `${id.slice(0, len)}…` : id;
}
