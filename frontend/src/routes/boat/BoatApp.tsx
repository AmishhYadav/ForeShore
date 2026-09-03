/**
 * `/boat` — the fisherman-facing surface. Tamil-first, voice-first, verdict-first: the
 * verdict card is the emotional/functional centre of the whole pitch (PLAN.md Phase 5).
 *
 * Two data paths, mirroring CLAUDE.md's architecture:
 *   - request path: postQuery() -> verdict + evidence panel + route + map, spoken back
 *     in whatever language the backend mirrored.
 *   - "No signal" path: no `shared/api.ts` call is made at all while `offline` is true.
 *     Geofence proximity keeps running from `navigator.geolocation` against the last
 *     cached polygons (`shared/geofenceCheck.ts`, turf, no network), and the verdict
 *     card falls back to the last cached decision — shown as cached, never as live, and
 *     not at all once it has passed its own `valid_to`.
 */
import { useEffect, useState } from "react";
import { ApiError, getGeofencesGeoJson, getRegion, postQuery } from "@shared/api";
import { activeVoiceAdapter } from "@shared/voice";
import {
  cacheDecision,
  cacheGeofences,
  cacheRoute,
  getCachedDecision,
  getCachedGeofences,
  isDecisionStillValid,
  setManualOffline,
} from "@shared/offline";
import type { CachedDecision } from "@shared/offline";
import type { QueryOutcome, RegionInfo } from "@shared/types";
import { VerdictCard } from "./VerdictCard";
import { ScenarioCompare } from "./ScenarioCompare";
import { EvidencePanel } from "./EvidencePanel";
import { VoiceInput } from "./VoiceInput";
import { MapView } from "./MapView";
import { OfflineToggle } from "./OfflineToggle";
import { AlertBanner } from "./AlertBanner";
import { RouteSummary } from "./RouteSummary";
import { useOwnPosition } from "./useOwnPosition";
import { useProximityAlerts } from "./useProximityAlerts";
import "./boat.css";

// Mirrors backend/foreshore/agents/synthesis.py's LABELS["en"] — used only before the
// first query has returned a real (possibly Tamil) `payloads.labels`, so the shell has
// something to render. Never used in place of a backend-mirrored label once one exists.
const DEFAULT_LABELS: Record<string, string> = {
  evidence: "Evidence",
  source: "source",
  why: "Why",
  ceiling: "Governing advisory",
  handoff: "Who to contact",
  downgraded: "This advisory was made more cautious",
  no_signal: "No signal — using the last saved advisory",
  boundaries: "Boundaries",
  route: "Route",
  unavailable: "not available",
};

export default function BoatApp() {
  const [region, setRegion] = useState<RegionInfo | null>(null);
  const [manualOffline, setManualOfflineLocal] = useState(false);
  const [browserOffline, setBrowserOffline] = useState(typeof navigator !== "undefined" ? !navigator.onLine : false);
  const offline = manualOffline || browserOffline;

  const position = useOwnPosition(region);

  const [outcome, setOutcome] = useState<QueryOutcome | null>(null);
  const [lastQuestion, setLastQuestion] = useState<string | null>(null);
  const [queryLoading, setQueryLoading] = useState(false);
  const [queryError, setQueryError] = useState<string | null>(null);

  const [geofenceFC, setGeofenceFC] = useState<GeoJSON.FeatureCollection | null>(null);
  const [cachedDecision, setCachedDecision] = useState<CachedDecision | undefined>(undefined);

  // -- bootstrap: region config -----------------------------------------------------------
  // Guarded by `offline` like every other network call: if the device is offline the
  // instant this mounts, this simply waits for `offline` to become false rather than
  // firing once regardless. (Cold-starting the whole app with zero prior cache while
  // genuinely offline is an inherent PWA limitation — there is no region config to fall
  // back to client-side until one online session has primed it.)
  useEffect(() => {
    if (offline || region) return;
    let cancelled = false;
    getRegion()
      .then((r) => {
        if (!cancelled) setRegion(r);
      })
      .catch((err) => console.warn("[BoatApp] failed to load region config:", err));
    return () => {
      cancelled = true;
    };
  }, [offline, region]);

  // -- browser connectivity signal --------------------------------------------------------
  useEffect(() => {
    const onOnline = () => setBrowserOffline(false);
    const onOffline = () => setBrowserOffline(true);
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);
    return () => {
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
    };
  }, []);

  // -- geofence polygons: instant paint from any prior cache, then refresh online -------
  useEffect(() => {
    let cancelled = false;
    getCachedGeofences()
      .then((fc) => {
        if (!cancelled && fc) setGeofenceFC((cur) => cur ?? fc);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (offline) return; // never call shared/api.ts while offline
    let cancelled = false;
    getGeofencesGeoJson()
      .then((fc) => {
        if (cancelled) return;
        setGeofenceFC(fc);
        cacheGeofences(fc).catch(() => {});
      })
      .catch((err) => console.warn("[BoatApp] failed to fetch geofences:", err));
    return () => {
      cancelled = true;
    };
  }, [offline]);

  // -- cached decision: kept warm at all times (a local IndexedDB read, not a network
  // call) so it is ready the instant the user flips "No signal" -------------------------
  useEffect(() => {
    let cancelled = false;
    getCachedDecision()
      .then((cd) => {
        if (!cancelled) setCachedDecision(cd);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [offline]);

  const proximity = useProximityAlerts({
    offline,
    position,
    geofenceGeoJson: geofenceFC,
    language: outcome?.language ?? region?.primary_language ?? "en",
  });

  function handleOfflineToggle(next: boolean) {
    setManualOffline(next);
    setManualOfflineLocal(next);
  }

  async function handleAsk(text: string) {
    if (offline) {
      setQueryError("No signal — new questions are unavailable right now. Showing the last saved advisory below.");
      return;
    }
    if (!position.ready) {
      setQueryError("Still finding your position — try again in a moment.");
      return;
    }
    setQueryLoading(true);
    setQueryError(null);
    setLastQuestion(text);
    try {
      const result = await postQuery({
        text,
        lat: position.lat,
        lon: position.lon,
        heading_deg: position.headingDeg ?? undefined,
        speed_kn: position.speedKn ?? undefined,
        surface: "boat",
      });
      setOutcome(result);
      cacheDecision(result).catch(() => {});
      if (result.route) cacheRoute(result.route).catch(() => {});
      if (activeVoiceAdapter.available) {
        activeVoiceAdapter
          .speak(result.text, result.language === "ta" ? "ta-IN" : "en-IN")
          .catch((err) => console.warn("[BoatApp] speech synthesis failed:", err));
      }
    } catch (err) {
      console.warn("[BoatApp] query failed:", err);
      setQueryError(
        err instanceof ApiError
          ? `FORESHORE could not answer that (HTTP ${err.status}).`
          : "Could not reach FORESHORE — check your connection.",
      );
    } finally {
      setQueryLoading(false);
    }
  }

  // Single source of truth for everything below the toggle: the live outcome when
  // online, or the cached one when offline and *still within its own validity window* —
  // an expired cached verdict is never treated as if it were current.
  const cachedStillValid = Boolean(cachedDecision && isDecisionStillValid(cachedDecision));
  const activeOutcome: QueryOutcome | null = offline ? (cachedStillValid ? cachedDecision!.outcome : null) : outcome;
  const activeLabels = activeOutcome?.payloads?.labels ?? DEFAULT_LABELS;
  const activeLanguage = activeOutcome?.language ?? region?.primary_language ?? "en";
  // Tolerant read: `docs/API.md`/`shared/types.ts` document `verdict_copy` as
  // `{headline, reason}`, but the running backend's `agents/synthesis.py::VERDICT_COPY`
  // actually emits `{headline, lead}` — no `reason` key exists at runtime. Prefer
  // whichever is present; flagged in the handoff report.
  const rawVerdictCopy = activeOutcome?.payloads?.verdict_copy as Record<string, unknown> | null | undefined;
  const activeCopy = rawVerdictCopy
    ? { headline: String(rawVerdictCopy.headline ?? ""), reason: String(rawVerdictCopy.reason ?? rawVerdictCopy.lead ?? "") }
    : null;

  let staleNotice: string | null = null;
  let emptyHeadline = "No advisory yet";
  let emptyMessage = "Ask a question — by voice or text — to get a verdict for right now.";
  if (offline) {
    if (cachedStillValid) {
      staleNotice = activeLabels.no_signal ?? DEFAULT_LABELS.no_signal;
    } else if (cachedDecision) {
      emptyHeadline = "Cached advisory expired";
      emptyMessage = "The last saved advisory has passed its validity window. Reconnect for a fresh one — FORESHORE will not show an out-of-date advisory as if it were current.";
    } else {
      emptyHeadline = "No cached advisory";
      emptyMessage = "Nothing saved on this device yet. Ask a question once you're back online so there's a fallback next time.";
    }
  }

  const positionNote =
    position.source === "gps"
      ? "Own position — GPS"
      : position.source === "anchor"
        ? `Own position — using ${region?.anchor_ports?.[0]?.name ?? "anchor port"} (no GPS fix)`
        : "Finding your position…";

  return (
    <div className="boat-app">
      <header className="boat-app__header">
        <div className="boat-app__brand">
          <span className="boat-app__brand-mark" aria-hidden="true" />
          FORESHORE
        </div>
        <OfflineToggle offline={offline} browserOffline={browserOffline} onChange={handleOfflineToggle} />
      </header>

      <main className="boat-app__main">
        {activeOutcome?.scenario ? (
          <ScenarioCompare scenario={activeOutcome.scenario} labels={activeLabels} />
        ) : (
          <VerdictCard
            verdict={activeOutcome?.verdict ?? null}
            copy={activeCopy}
            labels={activeLabels}
            stale={offline}
            staleNotice={staleNotice}
            emptyHeadline={emptyHeadline}
            emptyMessage={emptyMessage}
          />
        )}

        <AlertBanner alerts={proximity.alerts} offline={offline} hasData={proximity.hasData} />

        <section className="ask-section">
          <VoiceInput
            onSubmit={handleAsk}
            disabled={offline || queryLoading || !position.ready}
            disabledReason={
              offline
                ? "No signal — new questions unavailable. Showing the last saved advisory."
                : !position.ready
                  ? "Finding your position before asking…"
                  : queryLoading
                    ? "Thinking…"
                    : undefined
            }
          />
          {queryError ? <div className="ask-section__error">{queryError}</div> : null}
          {!offline && lastQuestion && activeOutcome ? (
            <div className="ask-section__transcript">
              <div className="ask-section__question">“{lastQuestion}”</div>
              <div className="ask-section__answer">{activeOutcome.text}</div>
            </div>
          ) : null}
        </section>

        <MapView
          region={region}
          position={position}
          geofenceGeoJson={geofenceFC}
          route={(activeOutcome?.route as unknown as Record<string, unknown> | undefined) ?? null}
        />

        {activeOutcome?.route ? (
          <RouteSummary
            route={activeOutcome.route as unknown as Record<string, unknown>}
            heading={activeLabels.route ?? DEFAULT_LABELS.route}
          />
        ) : null}

        {activeOutcome ? (
          <EvidencePanel
            rows={(activeOutcome.payloads?.evidence_panel ?? []) as unknown as Record<string, unknown>[]}
            heading={offline ? `${activeLabels.evidence ?? DEFAULT_LABELS.evidence} — cached` : activeLabels.evidence ?? DEFAULT_LABELS.evidence}
            unsourcedNumbers={activeOutcome.unsourced_numbers}
          />
        ) : null}
      </main>

      <footer className="boat-app__footer">
        <span>{positionNote}</span>
        {position.error ? <span className="boat-app__position-error">{position.error}</span> : null}
        <span className="boat-app__lang">{activeLanguage.toUpperCase()}</span>
      </footer>
    </div>
  );
}
