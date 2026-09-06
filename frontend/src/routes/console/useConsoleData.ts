/**
 * All console data-fetching and live-update wiring in one hook, so ConsoleApp.tsx and
 * its children stay render-only.
 *
 * Two update paths feed the fleet + alert state, deliberately both wired (this is the
 * bug class PLAN.md's acceptance bar calls out — "only one of REST fetch or WS live
 * update actually working"):
 *   1. Initial paint: `getFleet()` / `getAlerts({active:true})` on mount, so the console
 *      shows something the instant it loads, before any socket handshake completes.
 *   2. Live updates: `AlertSocket` — "vessels" messages replace the fleet snapshot,
 *      "alert" messages are upserted (by `alert_id`) into the alert list.
 * A slow REST poll (20s) runs alongside the socket as a backstop, so a silently dead
 * WS connection still keeps the console correct — never the sole update path, since the
 * socket is much faster (5s fixture-mode tick / 60s live) and is what the push-loop
 * latency acceptance bar ("appears on the console within 5s") depends on.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ackAlert,
  getAlerts,
  getArchitecture,
  getFleet,
  getGeofencesGeoJson,
  getRegion,
  getTraces,
  setActiveRegion,
} from "@shared/api";
import { AlertSocket } from "@shared/ws";
import type { Alert, ArchitectureSpecialist, RegionInfo, VesselState } from "@shared/types";

export interface TraceListRow {
  query_id: string;
  started_at: string;
  agents: string[];
  step_count: number;
  tools: string[];
  question: string | null;
  surface: string | null;
  verdict: string | null;
  duration_ms: number;
  tool_ms: number;
  ok: boolean;
  [key: string]: unknown;
}

interface WsHello {
  interval_s: number;
  mode: "live" | "fixture";
  region_id: string | null;
}

const REST_POLL_MS = 20_000;
const TRACE_POLL_MS = 15_000;

function upsertAlert(list: Alert[], incoming: Alert): Alert[] {
  const idx = list.findIndex((a) => a.alert_id === incoming.alert_id);
  if (idx === -1) return [incoming, ...list];
  const next = list.slice();
  next[idx] = incoming;
  return next;
}

export function useConsoleData() {
  const [region, setRegion] = useState<RegionInfo | null>(null);
  const [vessels, setVessels] = useState<VesselState[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [geofences, setGeofences] = useState<GeoJSON.FeatureCollection | null>(null);
  const [architecture, setArchitecture] = useState<ArchitectureSpecialist[]>([]);
  const [traces, setTraces] = useState<TraceListRow[]>([]);
  const [wsHello, setWsHello] = useState<WsHello | null>(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [vesselsUpdatedAt, setVesselsUpdatedAt] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  /** Alerts acknowledged from this console since it was opened, newest first.
   *
   *  Session-scoped on purpose. The backend does keep every acknowledgement — the alert
   *  stays in `AlertStore.history()` with its `acknowledged_by`/`acknowledged_at` — but
   *  that history is an in-memory rolling log of the last 500 alerts with no count or
   *  filter endpoint, so reading it back on every poll would mean pulling ~500 records to
   *  derive one number. The panel labels this "this session" rather than implying a
   *  durable total it is not showing. */
  const [acknowledged, setAcknowledged] = useState<Alert[]>([]);

  const socketRef = useRef<AlertSocket | null>(null);
  const lastMessageAtRef = useRef<number>(0);
  const helloIntervalSRef = useRef<number>(10);
  /** alert_id -> local arrival time, for alerts pushed over the socket. Read by
   *  `refreshAlerts` to tell "the server has not heard of this yet" apart from "the
   *  server has dropped this". */
  const alertSeenAtRef = useRef<Map<string, number>>(new Map());

  const refreshTraces = useCallback(async () => {
    try {
      const res = await getTraces(20);
      setTraces(res.queries as TraceListRow[]);
    } catch {
      // Trace history is a secondary panel — a failed refresh should not disturb the
      // fleet map or alert queue, which are the primary operational surfaces.
    }
  }, []);

  const refreshFleet = useCallback(async () => {
    try {
      const res = await getFleet();
      setVessels(res.vessels);
      setVesselsUpdatedAt(res.generated_at);
    } catch {
      // Leave the last-known fleet snapshot on screen rather than blanking it.
    }
  }, []);

  const refreshAlerts = useCallback(async () => {
    // Captured *before* the request goes out, so the "arrived while this poll was in
    // flight" test below can never be satisfied by an alert the server already knew
    // about and deliberately omitted.
    const startedAt = Date.now();
    try {
      const res = await getAlerts({ active: true });
      setAlerts((prev) => {
        // The server's active set is authoritative. `AlertStore.acknowledge()` and
        // `AlertStore.clear()` both DROP an alert from that set (see
        // backend/foreshore/push/alerts.py), so an alert the server no longer lists has
        // been acknowledged or has stopped applying, and must leave the queue.
        //
        // This previously merged into `prev` without ever removing anything, so the
        // console accumulated every alert it had ever seen: a queue reading "79 active,
        // 63 critical" against a backend holding 4, and an Acknowledge button whose
        // effect the next poll silently undid.
        const byId = new Map<string, Alert>();
        for (const a of res.alerts) byId.set(a.alert_id, a);
        // The one thing the server's answer legitimately cannot contain: an alert pushed
        // over the socket after this request was issued. Keep only those.
        for (const a of prev) {
          if (byId.has(a.alert_id)) continue;
          if ((alertSeenAtRef.current.get(a.alert_id) ?? 0) > startedAt) byId.set(a.alert_id, a);
        }
        // Drop bookkeeping for alerts no longer held, so the map cannot grow unbounded
        // across a long console session.
        for (const id of alertSeenAtRef.current.keys()) {
          if (!byId.has(id)) alertSeenAtRef.current.delete(id);
        }
        return Array.from(byId.values());
      });
    } catch {
      // Same reasoning as refreshFleet — degrade to stale-but-present, not blank.
    }
  }, []);

  const ack = useCallback(async (alertId: string, by: string) => {
    // `AlertStore.acknowledge()` stamps `acknowledged_at`/`acknowledged_by`, removes the
    // alert from the active set, and returns it — the returned object is the record of
    // who took responsibility and when. Mirror that split here: out of the open queue,
    // into the acknowledged log, so the operator's action visibly lands somewhere instead
    // of the row simply vanishing.
    const acknowledged = await ackAlert(alertId, by);
    setAlerts((prev) => prev.filter((a) => a.alert_id !== alertId));
    alertSeenAtRef.current.delete(alertId);
    setAcknowledged((prev) => [acknowledged, ...prev.filter((a) => a.alert_id !== alertId)]);
  }, []);

  // Region swap — PLAN.md Phase 7 item 3 / RegionSwitcher.tsx. Flips the backend's
  // process-wide active region, then re-fetches the two things that visibly depend on it
  // (region info and the geofence layer, explicitly passing region_id even though the
  // backend now defaults to it too, per the brief). Deliberately does NOT touch
  // vessels/alerts — the simulated fleet stays in Palk Bay regardless of the active
  // region, a known and intentional scope boundary (see RegionSwitcher.tsx). Propagates
  // any failure to the caller (RegionSwitcher) rather than swallowing it, so the UI can
  // show a real error state instead of silently no-op'ing.
  const swapRegion = useCallback(async (regionId: string) => {
    const newRegion = await setActiveRegion(regionId);
    setRegion(newRegion);
    try {
      const geoRes = await getGeofencesGeoJson(undefined, regionId);
      setGeofences(geoRes);
    } catch {
      // Non-fatal: the region swap itself succeeded even if the geofence re-fetch
      // failed — leave the last-known geofence layer on screen rather than blanking it,
      // same degrade-gracefully pattern as refreshFleet/refreshAlerts above.
    }
    return newRegion;
  }, []);

  // -- initial paint ----------------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [regionRes, fleetRes, alertsRes, geoRes, archRes] = await Promise.all([
          getRegion(),
          getFleet(),
          getAlerts({ active: true }),
          getGeofencesGeoJson(),
          getArchitecture(),
        ]);
        if (cancelled) return;
        setRegion(regionRes);
        setVessels(fleetRes.vessels);
        setVesselsUpdatedAt(fleetRes.generated_at);
        setAlerts(alertsRes.alerts);
        setGeofences(geoRes);
        setArchitecture(archRes.specialists);
      } catch (err) {
        if (!cancelled) {
          setLoadError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    refreshTraces();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // -- live updates: WebSocket -------------------------------------------------------
  useEffect(() => {
    const socket = new AlertSocket();
    socketRef.current = socket;
    const unsubscribe = socket.onMessage((msg) => {
      lastMessageAtRef.current = Date.now();
      setWsConnected(true);
      switch (msg.type) {
        case "hello":
          setWsHello({ interval_s: msg.interval_s, mode: msg.mode, region_id: msg.region_id });
          helloIntervalSRef.current = msg.interval_s;
          break;
        case "vessels":
          setVessels(msg.vessels);
          setVesselsUpdatedAt(msg.ts);
          break;
        case "alert":
          alertSeenAtRef.current.set(msg.alert.alert_id, Date.now());
          setAlerts((prev) => upsertAlert(prev, msg.alert));
          break;
      }
    });
    socket.connect();
    socket.subscribe([]); // empty = every vessel, the console's whole-fleet view

    // Heuristic connection watchdog: AlertSocket does not expose an open/close callback
    // beyond onMessage, so "connected" is inferred from message recency against the
    // server's own declared tick interval (from "hello"), falling back to a fixed
    // threshold before hello has ever arrived.
    const watchdog = window.setInterval(() => {
      const intervalMs = helloIntervalSRef.current * 1000;
      const threshold = Math.max(intervalMs * 3, 15_000);
      if (lastMessageAtRef.current > 0 && Date.now() - lastMessageAtRef.current > threshold) {
        setWsConnected(false);
      }
    }, 5_000);

    return () => {
      window.clearInterval(watchdog);
      unsubscribe();
      socket.disconnect();
      socketRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // -- REST backstop poll ------------------------------------------------------------
  useEffect(() => {
    const fleetTimer = window.setInterval(refreshFleet, REST_POLL_MS);
    const alertsTimer = window.setInterval(refreshAlerts, REST_POLL_MS);
    const traceTimer = window.setInterval(refreshTraces, TRACE_POLL_MS);
    return () => {
      window.clearInterval(fleetTimer);
      window.clearInterval(alertsTimer);
      window.clearInterval(traceTimer);
    };
  }, [refreshFleet, refreshAlerts, refreshTraces]);

  return {
    region,
    vessels,
    alerts,
    geofences,
    architecture,
    traces,
    wsHello,
    wsConnected,
    vesselsUpdatedAt,
    loadError,
    acknowledged,
    ack,
    refreshTraces,
    swapRegion,
  };
}
