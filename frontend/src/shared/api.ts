/**
 * Typed HTTP client for docs/API.md. Every route module (/boat, /console) calls the
 * backend only through this file — no ad-hoc `fetch()` calls elsewhere, so the contract
 * stays enforced in one place.
 */
import type {
  Alert,
  AlertLevel,
  ConditionsPayload,
  HazardsPayload,
  HealthReport,
  PfzDerivedPayload,
  PfzOfficialPayload,
  ProductiveWatersPayload,
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

// -- Streaming request path ------------------------------------------------------------

export interface QueryStreamHandlers {
  onStatus?: (s: { phase: string; detail: string; query_id: string }) => void;
  onToken?: (delta: string) => void;
  onDone: (outcome: QueryOutcome) => void;
  onError: (message: string) => void;
}

/**
 * POST /api/query/stream — same body as postQuery, but the response is an SSE stream of
 * `status` / `token` / `done` / `error` events (see docs/API.md). Parses the stream by
 * hand (fetch + getReader + TextDecoder) rather than EventSource, since EventSource can't
 * send a POST body.
 *
 * Never throws for a deliberate abort — `signal`-triggered cancellation resolves quietly.
 * Any other failure (network error, non-2xx, a mid-stream read error) calls
 * `handlers.onError` exactly once. Exactly one of onDone/onError fires per call, and the
 * returned promise always settles.
 */
export function streamQuery(
  body: QueryRequest,
  handlers: QueryStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  return (async () => {
    let res: Response;
    try {
      res = await fetch(`${API_BASE}/api/query/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal,
      });
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      handlers.onError(err instanceof Error ? err.message : String(err));
      return;
    }

    if (!res.ok || !res.body) {
      let responseBody: unknown;
      try {
        responseBody = await res.json();
      } catch {
        try {
          responseBody = await res.text();
        } catch {
          responseBody = null;
        }
      }
      handlers.onError(`API ${res.status}: ${JSON.stringify(responseBody)}`);
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let settled = false;

    // One SSE block looks like:
    //   event: token
    //   data: {"delta":"..."}
    //   <blank line>
    // Lines can use \n or \r\n, and a read() chunk boundary can land anywhere — including
    // mid-line inside the JSON string on `data:`. So we only ever act on a complete block
    // (delimited by a blank line), buffering everything before that.
    function dispatchBlock(block: string) {
      let eventName = "message";
      const dataLines: string[] = [];
      for (const rawLine of block.split("\n")) {
        const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
        if (line.startsWith("event:")) {
          eventName = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trim());
        }
      }
      if (dataLines.length === 0) return;
      const raw = dataLines.join("\n");

      let payload: unknown;
      try {
        payload = JSON.parse(raw);
      } catch {
        return; // malformed data line — skip, not fatal
      }

      switch (eventName) {
        case "status":
          handlers.onStatus?.(payload as { phase: string; detail: string; query_id: string });
          break;
        case "token":
          handlers.onToken?.((payload as { delta: string }).delta);
          break;
        case "done":
          settled = true;
          handlers.onDone(payload as QueryOutcome);
          break;
        case "error":
          settled = true;
          handlers.onError((payload as { error: string }).error);
          break;
        default:
          break;
      }
    }

    try {
      while (!settled) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // Normalise CRLF so the "\n\n" separator check below is a single case.
        buffer = buffer.replace(/\r\n/g, "\n");
        let sepIndex: number;
        while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
          const block = buffer.slice(0, sepIndex);
          buffer = buffer.slice(sepIndex + 2);
          if (block.trim().length > 0) dispatchBlock(block);
          if (settled) break;
        }
      }
      if (!settled) {
        // Stream ended (or loop broke) with no terminal event — treat as a hang, not a
        // silent success, so the caller's UI doesn't sit forever on "Asking…".
        handlers.onError("Stream ended before a result arrived.");
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      if (!settled) handlers.onError(err instanceof Error ? err.message : String(err));
    } finally {
      try {
        reader.releaseLock();
      } catch {
        // already released
      }
    }
  })();
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

/**
 * POST /api/alerts/broadcast — a console-authored alert, pushed live over the exact same
 * WS /ws/alerts transport the automated geofence/weather/hazard alerts use. `vessel_id`
 * omitted broadcasts to every tracked vessel. Every client already subscribed to that
 * socket (boat UI, another console tab, a phone running either) renders it the instant
 * this resolves — no polling.
 */
export function broadcastAlert(body: {
  vessel_id?: string | null;
  level?: AlertLevel;
  title: string;
  body: string;
  by?: string;
}): Promise<{ broadcast_id: string; sent: number; alerts: Alert[] }> {
  return request("/api/alerts/broadcast", { method: "POST", body: JSON.stringify(body) });
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

export function getGeofencesGeoJson(classes?: string[], regionId?: string): Promise<GeoJSON.FeatureCollection> {
  const q = new URLSearchParams();
  if (classes && classes.length) q.set("classes", classes.join(","));
  if (regionId) q.set("region_id", regionId);
  const qs = q.toString();
  return request(`/api/geofences.geojson${qs ? `?${qs}` : ""}`);
}

// -- Raw conditions dashboard (console Data tab) ----------------------------------------

export function getConditions(lat: number, lon: number, when?: string): Promise<ConditionsPayload> {
  const q = new URLSearchParams({ lat: String(lat), lon: String(lon) });
  if (when) q.set("when", when);
  return request(`/api/conditions?${q.toString()}`);
}

// -- Map-layer passthroughs (tools 7, 8, 12) -------------------------------------------

export function getPfzOfficial(lat: number, lon: number): Promise<ToolResultEnvelope<PfzOfficialPayload>> {
  const q = new URLSearchParams({ lat: String(lat), lon: String(lon) });
  return request(`/api/pfz/official?${q.toString()}`);
}

export function getPfzDerived(params?: {
  bbox?: [number, number, number, number];
  when?: string;
}): Promise<ToolResultEnvelope<PfzDerivedPayload>> {
  const q = new URLSearchParams();
  if (params?.bbox) q.set("bbox", params.bbox.join(","));
  if (params?.when) q.set("when", params.when);
  const qs = q.toString();
  return request(`/api/pfz/derived${qs ? `?${qs}` : ""}`);
}

export function getHazards(params?: {
  bbox?: [number, number, number, number];
  when?: string;
}): Promise<ToolResultEnvelope<HazardsPayload>> {
  const q = new URLSearchParams();
  if (params?.bbox) q.set("bbox", params.bbox.join(","));
  if (params?.when) q.set("when", params.when);
  const qs = q.toString();
  return request(`/api/hazards${qs ? `?${qs}` : ""}`);
}

// -- Map-layer passthrough (tool 18) ----------------------------------------------------

export function getProductiveWaters(params?: {
  bbox?: [number, number, number, number];
  when?: string;
  lat?: number;
  lon?: number;
  limit?: number;
}): Promise<ToolResultEnvelope<ProductiveWatersPayload>> {
  const q = new URLSearchParams();
  if (params?.bbox) q.set("bbox", params.bbox.join(","));
  if (params?.when) q.set("when", params.when);
  if (params?.lat !== undefined) q.set("lat", String(params.lat));
  if (params?.lon !== undefined) q.set("lon", String(params.lon));
  if (params?.limit !== undefined) q.set("limit", String(params.limit));
  const qs = q.toString();
  return request(`/api/productive-waters${qs ? `?${qs}` : ""}`);
}

// -- Region swap ------------------------------------------------------------------------

/** POST /api/region/active — swaps the process-wide active region (config.set_active_region),
 * so every subsequent call that doesn't pass its own region_id (fleet, geofences, push loop)
 * re-homes too. A single POST /api/query can already target a region without this, via its
 * own region_id field — this exists only for the surfaces that don't take one per call. */
export function setActiveRegion(regionId: string): Promise<RegionInfo> {
  return request("/api/region/active", { method: "POST", body: JSON.stringify({ region_id: regionId }) });
}

export { ApiError };
