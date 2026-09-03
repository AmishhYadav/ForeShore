/**
 * IndexedDB cache for the offline path — PLAN.md Phase 5, locked decision: geofence
 * proximity checking must run client-side from `navigator.geolocation` with no network,
 * because "offshore connectivity beyond ~10-12km" is a named open unknown (CLAUDE.md).
 * This module caches exactly the four things PLAN.md names: geofence polygons, the last
 * decision envelope with its validity window, the last route, and pre-rendered TTS
 * phrases. Nothing else — this is not a generic cache.
 */
import type { QueryOutcome, RouteShape } from "./types";

const DB_NAME = "foreshore-offline";
const DB_VERSION = 1;
const STORES = ["geofences", "decision", "route", "ttsPhrases"] as const;
type StoreName = (typeof STORES)[number];

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      for (const store of STORES) {
        if (!db.objectStoreNames.contains(store)) db.createObjectStore(store);
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function put(store: StoreName, key: string, value: unknown): Promise<void> {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(store, "readwrite");
    tx.objectStore(store).put(value, key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function get<T>(store: StoreName, key: string): Promise<T | undefined> {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(store, "readonly");
    const req = tx.objectStore(store).get(key);
    req.onsuccess = () => resolve(req.result as T | undefined);
    req.onerror = () => reject(req.error);
  });
}

// -- 1. Geofence polygons (GeoJSON FeatureCollection, from /api/geofences.geojson) ----

export function cacheGeofences(geojson: GeoJSON.FeatureCollection): Promise<void> {
  return put("geofences", "current", geojson);
}

export function getCachedGeofences(): Promise<GeoJSON.FeatureCollection | undefined> {
  return get("geofences", "current");
}

// -- 2. Last decision envelope, with validity window ------------------------------------

export interface CachedDecision {
  outcome: QueryOutcome;
  cachedAt: string;
}

export function cacheDecision(outcome: QueryOutcome): Promise<void> {
  return put("decision", "last", { outcome, cachedAt: new Date().toISOString() });
}

export function getCachedDecision(): Promise<CachedDecision | undefined> {
  return get("decision", "last");
}

/** A cached verdict is only usable while its own validity window (`valid_to`) holds —
 * the 12h IMD bulletin rule applies just as much to a cached read as a live one. */
export function isDecisionStillValid(cached: CachedDecision): boolean {
  const validTo = cached.outcome.verdict?.valid_to;
  if (!validTo) return false;
  return new Date(validTo).getTime() > Date.now();
}

// -- 3. Last route ------------------------------------------------------------------

export function cacheRoute(route: RouteShape): Promise<void> {
  return put("route", "last", route);
}

export function getCachedRoute(): Promise<RouteShape | undefined> {
  return get("route", "last");
}

// -- 4. Pre-rendered TTS phrases ------------------------------------------------------
// Common verdict-card phrases, pre-synthesized as audio blobs while online so the boat
// UI can still speak a verdict with zero network (Web Speech synthesis itself needs no
// network on most platforms, but this covers browsers/voices that do fetch a remote
// voice model, and doubles as a cache for Bhashini TTS once that adapter is live).
//
// Currently unpopulated by design, not by oversight: `WebSpeechVoiceAdapter.speak()`
// (shared/voice.ts) drives `window.speechSynthesis` directly, which has no API to hand
// back the rendered audio as a Blob to cache — and `BhashiniVoiceAdapter`, the one
// adapter whose network call *would* return cacheable audio bytes, is still a stub that
// throws (CLAUDE.md open unknown: Bhashini/ULCA registration has not resolved). Wire
// `cacheTtsPhrase` into `BhashiniVoiceAdapter.speak()` once that adapter is real.

export function cacheTtsPhrase(key: string, audioBlob: Blob): Promise<void> {
  return put("ttsPhrases", key, audioBlob);
}

export function getCachedTtsPhrase(key: string): Promise<Blob | undefined> {
  return get("ttsPhrases", key);
}

// -- online/offline signal ------------------------------------------------------------
// Wraps navigator.onLine plus the demo's manual "No signal" toggle (PLAN.md 3:15 beat:
// "Flip 'No signal' — alert still fires"). A UI component owns the toggle's checkbox
// state; this just gives both a common source of truth to read.

let manualOffline = false;

export function setManualOffline(value: boolean): void {
  manualOffline = value;
}

export function isOffline(): boolean {
  return manualOffline || !navigator.onLine;
}
