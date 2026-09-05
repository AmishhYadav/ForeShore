/**
 * Wire types mirroring docs/API.md and backend/foreshore/models.py's `.to_dict()` shapes.
 * This file is the one place the frontend's understanding of the backend contract lives —
 * both /boat and /console import from here, never redefine their own copy of a shape.
 * Keep it in lockstep with docs/API.md; if a backend `.to_dict()` changes, this is the
 * file to fix first.
 */

export type VerdictLevel = "GO" | "GO_WITH_CAUTION" | "DO_NOT_ADVISE";

/** Uniform envelope every tool in the registry returns — ToolResult.to_dict(). */
export interface ToolResultEnvelope<TPayload = Record<string, unknown>> {
  tool: string;
  ok: boolean;
  summary: string;
  observations: Observation[];
  payload: TPayload;
  error: string | null;
  partial: boolean;
  missing: string[];
}

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

export type AlertLevel = "INFO" | "WARN" | "CRITICAL" | "BREACH";

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

/** One alternate landing centre offered alongside the primary handoff. */
export interface HandoffAlternate {
  authority_name: string;
  authority_type: string;
  district?: string | null;
  contact: string | null;
  contact_label?: string | null;
  /** See `contact_verified` on Handoff — same rule applies. */
  contact_verified?: boolean;
  distance_nm: number | null;
}

export interface Handoff {
  authority_name: string;
  authority_type: "landing_centre" | "coast_guard" | "fisheries_office" | "port_office";
  contact: string | null;
  /** What the number reaches — "Harbour control", "Fisheries control room". */
  contact_label?: string | null;
  /**
   * True only for a real, published number (currently just Coast Guard 1554). Numbers
   * from the demo directory in `config/handoff_contacts.yaml` arrive with `false` and
   * MUST be rendered as plain text, never as a `tel:` link — a placeholder number
   * dialled in an emergency is the worst failure the abstention path can have.
   */
  contact_verified?: boolean;
  vhf_channel?: string | null;
  district?: string | null;
  distance_nm: number | null;
  alternates?: HandoffAlternate[];
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

/** One row per observation — agents/synthesis.py's EvidenceRow.to_dict(). `display` is
 * the value already formatted with its unit ("0.59 m", "MODERATE") — never re-derive a
 * bare number out of it client-side; render it as the label it is. */
export interface EvidencePanelRow {
  variable: string;
  display: string;
  source_name: string;
  authority: Authority;
  resolution: string;
  freshness: Freshness;
  acquired_at: string;
  is_derived: boolean;
  governs: boolean;
  /** Join key against TraceStep.provenance_ids — same "<source_id>@<timestamp>" format. */
  provenance_id: string;
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

/** One node of the stored reasoning trace — models.py's TraceStep.to_dict(). */
export interface TraceStep {
  step_id: string;
  query_id: string;
  parent_id: string | null;
  agent: string;
  kind: "plan" | "tool_call" | "tool_result" | "synthesis" | "ceiling" | "error";
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

/** GET /api/trace/{query_id} response shape — store/traces.py's TraceStore.tree(),
 * nested by parent_id rather than a flat list. */
export interface TraceTreeNode {
  step: TraceStep;
  children: TraceTreeNode[];
}

/** models.py's RouteLeg.to_dict(). */
export interface RouteLeg {
  from: [number, number];
  to: [number, number];
  distance_nm: number;
  bearing_deg: number;
  eta_seconds: number;
  cost_breakdown: Record<string, number>;
  note: string | null;
}

/** models.py's Route.to_dict(). No `distance_nm`/`eta`/`why_it_bends` fields — the
 * closest equivalents are `total_distance_nm`/`total_eta_seconds`/`avoided` below. */
export interface RouteShape {
  waypoints: [number, number][];
  legs: RouteLeg[];
  total_distance_nm: number;
  direct_distance_nm: number;
  detour_pct: number;
  total_eta_seconds: number;
  cost_breakdown: Record<string, number>;
  /** Names of hazards/boundaries the route steered around — the "why it bends" list. */
  avoided: string[];
  feasible: boolean;
  failure_reason: string | null;
  departure: string | null;
  vessel_class: string | null;
  evidence: Observation[];
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
    /** VERDICT_COPY[level][language] — null only when no verdict was evaluated. */
    verdict_copy: { headline: string; lead: string } | null;
    /** The answer before the final editor pass, and whether that pass ran. A rewrite
     * that moved a number, the verdict, the named handoff or the language is discarded
     * server-side and `reason` says which check failed — so `applied: false` means
     * `text` is already this string. */
    unpolished_text?: string;
    polish?: { applied: boolean; reason: string | null };
    [toolPayloadKey: string]: unknown;
  };
  unsourced_numbers: string[];
  plan: Plan;
  specialists_used: string[];
  missing: string[];
  duration_ms: number;
  architecture: ArchitectureSpecialist[];
  /** PLAN.md Phase 7 item 4 — populated only when the question named two explicit
   * departure times ("what if I leave at 04:00 instead of 06:00") and the request
   * carried no explicit `when`. `null` on every ordinary answer. */
  scenario: ScenarioComparison | null;
}

/** One side of a scenario comparison — orchestrator.py's ScenarioOption.to_dict(). */
export interface ScenarioOption {
  label: string;
  when: string;
  /** A complete, independent QueryOutcome for this departure time — recursive, but
   * always has its own `scenario` as `null` (a scenario option is never itself a
   * scenario). */
  outcome: QueryOutcome;
}

/** orchestrator.py's ScenarioComparison.to_dict(). */
export interface ScenarioComparison {
  /** Always exactly 2, earlier departure first. */
  options: ScenarioOption[];
  /** Plain-language bullets of what actually changed between the two options. */
  differences: string[];
  /** Index into `options` of the more permissive still-actionable choice. */
  recommended_index: number;
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
  updated_at: string;
  home_port: string | null;
  crew: number | null;
  is_simulated: true;
  last_verdict: VerdictLevel | null;
}

/** WS /ws/alerts server -> client messages. Per-vessel verdict rides in
 * `vessels[i].last_verdict` — nothing on the wire ever sends a standalone
 * `{type: "verdict", ...}` message (see docs/API.md). */
export type WsServerMessage =
  | { type: "hello"; interval_s: number; mode: "live" | "fixture"; region_id: string | null }
  | { type: "alert"; alert: Alert }
  | { type: "vessels"; vessels: VesselState[]; ts: string };

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

/** GET /api/pfz/official — tool 7's payload. `geometry`/`closest_point` are null (and
 * `missing` carries "incois_pfzlines") on days INCOIS has not published a line for this
 * sector — a valid, non-error outcome per CLAUDE.md, render as "no official line today"
 * rather than treating a null geometry as a failure. */
export interface PfzOfficialPayload {
  distance_nm: number | null;
  bearing_deg: number | null;
  advisory_date: string | null;
  closest_point: [number, number] | null;
  geometry: GeoJSON.Geometry | null;
  is_official: true;
}

/** GET /api/pfz/derived — tool 8's payload. Always label this as FORESHORE's own
 * indicative derivation, never as an official INCOIS product — see `disclaimer` and
 * CLAUDE.md's "Do not present derived PFZ zones as the official INCOIS advisory." */
export interface PfzDerivedPayload {
  zones: GeoJSON.FeatureCollection;
  method: Record<string, unknown>;
  granule: Record<string, unknown>;
  chlorophyll_available: boolean;
  /** One short human sentence, safe to render. */
  chlorophyll_reason: string | null;
  /** Full untruncated failure text — trace/debug only, never rendered in the boat UI. */
  chlorophyll_reason_detail?: string | null;
  disclaimer: string;
}

/** GET /api/hazards — tool 12's payload. `polygons` (cone/wind-radii exclusion areas)
 * and `cyclone_track` (the storm's own observed+forecast line) are deliberately two
 * separate GeoJSON collections — render as different map layers, not one. Each feature
 * carries `hazard_class`/`event_name`/`alert_level` in its `properties`.
 * `no_active_hazard: true` with empty collections is a valid, positive result. */
export interface HazardsPayload {
  events: Record<string, unknown>[];
  polygons: GeoJSON.Feature[];
  cyclone_track: GeoJSON.FeatureCollection;
  no_active_hazard: boolean;
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
