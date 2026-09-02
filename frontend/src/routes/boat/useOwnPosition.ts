/**
 * The user's own live device position — never the simulated fleet. Sourced from
 * `navigator.geolocation.watchPosition` (pure local device API, no network — safe to
 * run while offline) with a fallback to the region's first anchor port when
 * geolocation is denied, unavailable, or has not produced a fix yet. Never resolves to
 * (0,0): callers should gate on `ready`.
 */
import { useEffect, useRef, useState } from "react";
import type { RegionInfo } from "@shared/types";

export interface OwnPosition {
  lat: number;
  lon: number;
  headingDeg: number | null;
  speedKn: number | null;
  accuracyM: number | null;
  source: "gps" | "anchor" | "pending";
  ready: boolean;
  error: string | null;
}

const MS_TO_KN = 1.943844;

export function useOwnPosition(region: RegionInfo | null): OwnPosition {
  const [state, setState] = useState<Omit<OwnPosition, "ready">>({
    lat: 0,
    lon: 0,
    headingDeg: null,
    speedKn: null,
    accuracyM: null,
    source: "pending",
    error: null,
  });
  const sourceRef = useRef<"gps" | "anchor" | "pending">("pending");
  sourceRef.current = state.source;

  // Fallback to the first anchor port once region config is known — but only while no
  // GPS fix has landed yet; a later region refresh must never clobber a live fix.
  useEffect(() => {
    const anchor = region?.anchor_ports?.[0];
    if (!anchor) return;
    if (sourceRef.current !== "gps") {
      setState((s) => (s.source === "gps" ? s : { ...s, lat: anchor.lat, lon: anchor.lon, source: "anchor" }));
    }
  }, [region]);

  useEffect(() => {
    if (!("geolocation" in navigator)) {
      setState((s) => ({ ...s, error: "Geolocation unavailable in this browser" }));
      return;
    }
    const onSuccess = (pos: GeolocationPosition) => {
      setState({
        lat: pos.coords.latitude,
        lon: pos.coords.longitude,
        headingDeg: pos.coords.heading ?? null,
        speedKn: pos.coords.speed !== null && pos.coords.speed !== undefined ? pos.coords.speed * MS_TO_KN : null,
        accuracyM: pos.coords.accuracy ?? null,
        source: "gps",
        error: null,
      });
    };
    const onError = (err: GeolocationPositionError) => {
      // Keep whatever fix/fallback we already had; just surface the error text.
      setState((s) => ({ ...s, error: err.message || "Location unavailable" }));
    };
    let watchId: number | null = null;
    try {
      watchId = navigator.geolocation.watchPosition(onSuccess, onError, {
        enableHighAccuracy: true,
        maximumAge: 15000,
        timeout: 10000,
      });
    } catch (err) {
      setState((s) => ({ ...s, error: err instanceof Error ? err.message : "Location unavailable" }));
    }
    return () => {
      if (watchId !== null) navigator.geolocation.clearWatch(watchId);
    };
  }, []);

  return { ...state, ready: state.source !== "pending" };
}
