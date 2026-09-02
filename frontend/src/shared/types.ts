/**
 * Wire types mirroring docs/API.md and backend/foreshore/models.py's `.to_dict()` shapes.
 * This file is the one place the frontend's understanding of the backend contract lives —
 * both /boat and /console import from here, never redefine their own copy of a shape.
 * Keep it in lockstep with docs/API.md; if a backend `.to_dict()` changes, this is the
 * file to fix first.
 */

export type VerdictLevel = "GO" | "GO_WITH_CAUTION" | "DO_NOT_ADVISE";

export type Authority =
  | "IMD"
  | "INCOIS"
  | "GDACS"
  | "OpenMeteo"
  | "VLIZ"
  | "GEBCO"
  | "Bhuvan"
  | "derived";

export type GeofenceClass =
  | "IMBL_HISTORIC_WATERS"
  | "IMBL_MARITIME_BOUNDARY"
  | "MPA"
  | "ECO_SENSITIVE"
  | "USER_DEFINED"
  | "HAZARD_EXCLUSION";

export type AlertLevel = "WARN" | "CRITICAL";

export type Freshness = "live" | "recent" | "stale" | "expired";

export interface Provenance {
  provenance_id: string;
  source_id: string;
  source_name: string;
  authority: Authority;
  url: string;
  acquired_at: string;
  issued_at: string | null;
  valid_from: string | null;
  valid_to: string | null;
  spatial_resolution_m: number | null;
  temporal_resolution_s: number | null;
  freshness: Freshness;
  is_derived: boolean;
  notes: string | null;
}

export interface Observation {
  variable: string;
  value: number | string | null;
  unit: string;
  lat: number;
  lon: number;
  valid_time: string;
  provenance: Provenance;
  qualifiers: Record<string, unknown>;
}

export interface Handoff {
  authority_name: string;
  authority_type: "landing_centre" | "coast_guard";
  contact: string;
  distance_nm: number | null;
}

export interface Verdict {
  level: VerdictLevel;
  reasons: string[];
  ceiling_applied: boolean;
  downgraded_from: VerdictLevel | null;
  ceiling_source: Provenance | null;
  handoff: Handoff | null;
  valid_from: string | null;
  valid_to: string | null;
}

export interface EvidencePanelRow {
  variable: string;
  value: number | string | null;
  unit: string;
  source_name: string;
  authority: Authority;
  acquired_at: string;
  issued_at: string | null;
  resolution_m: number | null;
  freshness: Freshness;
  is_derived: boolean;
  governs: boolean;
}

export interface PlanStep {
  specialist: string;
  tool: string;
  args: Record<string, unknown>;
  why: string;
}

export interface Plan {
  steps: PlanStep[];
}

export interface TraceStep {
  step_id: string;
  parent: string | null;
  agent: string;
  tool: string | null;
  args: Record<string, unknown>;
  result_digest: string;
  provenance_ids: string[];
  duration_ms: number;
  ts: string;
  [key: string]: unknown;
}

export interface RouteLeg {
  from: [number, number];
  to: [number, number];
  distance_nm: number;
  cost_breakdown: Record<string, number>;
}

export interface RouteShape {
  waypoints: [number, number][];
  distance_nm: number;
  eta: string | null;
  legs: RouteLeg[];
  why_it_bends: string[];
}

export interface ArchitectureSpecialist {
  name: string;
  role: string;
  ps_capability: string;
  tools: string[];
}

/** POST /api/query request body. */
export interface QueryRequest {
  text: string;
  lat?: number;
  lon?: number;
  when?: string;
  vessel_class?: string;
  heading_deg?: number;
  speed_kn?: number;
  destination?: [number, number];
  language?: string | null;
  region_id?: string | null;
  surface: "boat" | "console";
  use_model?: boolean;
}

/** POST /api/query response — QueryOutcome.to_dict(). */
export interface QueryOutcome {
  query_id: string;
  language: string;
  text: string;
  verdict: Verdict | null;
  evidence: Observation[];
  trace: TraceStep[];
  route: RouteShape | null;
  payloads: {
    evidence_panel: EvidencePanelRow[];
    labels: Record<string, string>;
    verdict_copy: { headline: string; reason: string };
    [toolPayloadKey: string]: unknown;
  };
  unsourced_numbers: string[];
  plan: Plan;
  specialists_used: string[];
  missing: string[];
  duration_ms: number;
  architecture: ArchitectureSpecialist[];
}

export interface Alert {
  alert_id: string;
  vessel_id: string;
  kind: "geofence" | "hazard" | "weather" | "verdict_change";
  level: AlertLevel;
  title: { en: string; ta: string };
  body: { en: string; ta: string };
  lat: number;
  lon: number;
  created_at: string;
  dedupe_key: string;
  geofence_class: GeofenceClass | null;
  distance_nm: number | null;
  eta_seconds: number | null;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  evidence: Observation[];
  handoff: Handoff | null;
}

export interface VesselState {
  vessel_id: string;
  name: string;
  lat: number;
  lon: number;
  heading_deg: number;
  speed_kn: number;
  vessel_class: string;
  is_simulated: true;
  updated_at: string;
  [key: string]: unknown;
}

/** WS /ws/alerts server -> client messages. */
export type WsServerMessage =
  | { type: "hello"; interval_s: number; mode: "live" | "fixture"; region_id: string | null }
  | { type: "alert"; alert: Alert }
  | { type: "vessels"; vessels: VesselState[]; ts: string }
  | { type: "verdict"; vessel_id: string; level: VerdictLevel };

/** WS /ws/alerts client -> server messages. */
export type WsClientMessage =
  | { type: "ack"; alert_id: string; by: string }
  | { type: "subscribe"; vessel_ids: string[] };

export interface RegionInfo {
  region_id: string;
  display_name_en: string;
  display_name_local: string;
  bbox: [number, number, number, number];
  anchor_ports: { name: string; lat: number; lon: number; district: string | null }[];
  primary_language: string;
  fallback_language: string;
  languages: string[];
  districts: string[];
  basemap: Record<string, unknown>;
  vessel_classes: {
    class_id: string;
    label_en: string;
    label_local: string;
    range_nm: number;
    loa_m: number;
    cruise_speed_kn: number;
    max_speed_kn: number;
    min_depth_m: number;
    crew_typical: number;
  }[];
}

export interface HealthSourceRow {
  source_id: string;
  ok: boolean;
  latency_ms: number | null;
  issued_at: string | null;
  freshness: Freshness | null;
}

export interface HealthReport {
  mode: "live" | "fixture";
  region_id: string | null;
  sources: HealthSourceRow[];
  tools_unavailable: Record<string, string>;
  checked_at: string;
  note?: string;
}
