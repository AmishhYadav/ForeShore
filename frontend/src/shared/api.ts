/**
 * Typed HTTP client for docs/API.md. Every route module (/boat, /console) calls the
 * backend only through this file — no ad-hoc `fetch()` calls elsewhere, so the contract
 * stays enforced in one place.
 */
import type {
  Alert,
  HealthReport,
  QueryOutcome,
  QueryRequest,
  RegionInfo,
  RouteShape,
  ToolResultEnvelope,
  TraceTreeNode,
  Verdict,
  VesselState,
} from "./types";

export const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

class ApiError extends Error {
  constructor(
    public status: number,
    public body: unknown,
  ) {
    super(`API ${status}: ${JSON.stringify(body)}`);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let body: unknown;
    try {
      body = await res.json();
    } catch {
      body = await res.text();
    }
    throw new ApiError(res.status, body);
  }
  return res.json() as Promise<T>;
}

// -- Request path --------------------------------------------------------------------

export function postQuery(body: QueryRequest): Promise<QueryOutcome> {
  return request<QueryOutcome>("/api/query", { method: "POST", body: JSON.stringify(body) });
}

export function postRoute(body: {
  origin_lat: number;
  origin_lon: number;
  dest_lat: number;
  dest_lon: number;
  departure?: string;
  vessel_class?: string;
}): Promise<ToolResultEnvelope<{ route: RouteShape | null }>> {
  return request("/api/route", { method: "POST", body: JSON.stringify(body) });
}

export function getVerdict(params: {
  lat: number;
  lon: number;
  vessel_class?: string;
  when?: string;
}): Promise<ToolResultEnvelope<{ verdict: Verdict }>> {
  const q = new URLSearchParams();
  q.set("lat", String(params.lat));
  q.set("lon", String(params.lon));
  if (params.vessel_class) q.set("vessel_class", params.vessel_class);
  if (params.when) q.set("when", params.when);
  return request(`/api/verdict?${q.toString()}`);
}

export function postGeofenceCheck(body: {
  lat: number;
  lon: number;
  heading_deg?: number;
  speed_kn?: number;
  classes?: string[];
}): Promise<ToolResultEnvelope> {
  return request("/api/geofence/check", { method: "POST", body: JSON.stringify(body) });
}

// -- Fleet and push path --------------------------------------------------------------

export function getFleet(): Promise<{ vessels: VesselState[]; generated_at: string }> {
  return request("/api/fleet");
}

export function getAlerts(params?: {
  vessel_id?: string;
  active?: boolean;
  since?: string;
}): Promise<{ alerts: Alert[] }> {
  const q = new URLSearchParams();
  if (params?.vessel_id) q.set("vessel_id", params.vessel_id);
  if (params?.active !== undefined) q.set("active", String(params.active));
  if (params?.since) q.set("since", params.since);
  const qs = q.toString();
  return request(`/api/alerts${qs ? `?${qs}` : ""}`);
}

export function ackAlert(alertId: string, by = "unknown"): Promise<Alert> {
  return request(`/api/alerts/${alertId}/ack`, { method: "POST", body: JSON.stringify({ by }) });
}

// -- Reference and explainability ------------------------------------------------------

export function getHealth(): Promise<HealthReport> {
  return request("/health");
}

export function getRegion(regionId?: string): Promise<RegionInfo> {
  const q = regionId ? `?region_id=${encodeURIComponent(regionId)}` : "";
  return request(`/api/region${q}`);
}

export function getArchitecture(): Promise<{
  specialists: { name: string; role: string; ps_capability: string; tools: string[] }[];
}> {
  return request("/api/architecture");
}

export function getCatalogue(): Promise<unknown> {
  return request("/api/catalogue");
}

export function getTraces(limit = 20): Promise<{ queries: Record<string, unknown>[] }> {
  return request(`/api/traces?limit=${limit}`);
}

export function getTrace(queryId: string): Promise<{ query_id: string; steps: TraceTreeNode[] }> {
  return request(`/api/trace/${queryId}`);
}

export function getLayers(): Promise<{ layers: Record<string, unknown>[] }> {
  return request("/api/layers");
}

export function getLayerGeoJson(layerId: string): Promise<GeoJSON.FeatureCollection> {
  return request(`/api/layers/${layerId}`);
}

export function getGeofencesGeoJson(classes?: string[]): Promise<GeoJSON.FeatureCollection> {
  const q = classes && classes.length ? `?classes=${classes.join(",")}` : "";
  return request(`/api/geofences.geojson${q}`);
}

export { ApiError };
