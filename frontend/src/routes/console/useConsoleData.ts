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
} from "@shared/api";
import { AlertSocket } from "@shared/ws";
import type { Alert, ArchitectureSpecialist, RegionInfo, VesselState } from "@shared/types";

export interface TraceListRow {
  query_id: string;
  started_at: string;
  agents: string[];
  step_count: number;
  tools: string[];
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

  const socketRef = useRef<AlertSocket | null>(null);
  const lastMessageAtRef = useRef<number>(0);
  const helloIntervalSRef = useRef<number>(10);

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
    try {
      const res = await getAlerts({ active: true });
      setAlerts((prev) => {
        // Merge rather than replace: a WS "alert" push that landed between polls must
        // not be dropped by an in-flight REST response that predates it.
        const byId = new Map(prev.map((a) => [a.alert_id, a]));
        for (const a of res.alerts) byId.set(a.alert_id, a);
        return Array.from(byId.values());
      });
    } catch {
      // Same reasoning as refreshFleet — degrade to stale-but-present, not blank.
    }
  }, []);

  const ack = useCallback(async (alertId: string, by: string) => {
    const updated = await ackAlert(alertId, by);
    setAlerts((prev) => upsertAlert(prev, updated));
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
    ack,
    refreshTraces,
  };
}
