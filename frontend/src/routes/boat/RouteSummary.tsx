/**
 * Small "why it bends" summary shown under the map when a query triggered routing.
 *
 * Reads `outcome.route` tolerantly: `docs/API.md`/`shared/types.ts` describe it as
 * `{waypoints, distance_nm, eta, legs, why_it_bends}`, but the running backend's
 * `Route.to_dict()` (backend/foreshore/models.py) actually emits
 * `{waypoints, legs, total_distance_nm, direct_distance_nm, detour_pct,
 * total_eta_seconds, cost_breakdown, avoided, feasible, failure_reason, ...}` — no
 * `why_it_bends` field exists at all. `avoided` (what the router routed around) is used
 * as the "why it bends" content instead; every number shown here is one the backend
 * already computed, never a client-side guess. Flagged in the handoff report.
 */
import { formatEtaSeconds } from "./format";

export function RouteSummary({ route, heading }: { route: Record<string, unknown> | null; heading: string }) {
  if (!route) return null;

  const distance =
    typeof route.total_distance_nm === "number"
      ? route.total_distance_nm
      : typeof route.distance_nm === "number"
        ? (route.distance_nm as number)
        : null;
  const direct = typeof route.direct_distance_nm === "number" ? route.direct_distance_nm : null;
  const detourPct = typeof route.detour_pct === "number" ? route.detour_pct : null;
  const etaSeconds = typeof route.total_eta_seconds === "number" ? route.total_eta_seconds : null;
  const avoided = Array.isArray(route.avoided) ? (route.avoided as unknown[]).map(String) : [];
  const feasible = route.feasible !== false;
  const failureReason = typeof route.failure_reason === "string" ? route.failure_reason : null;

  if (!feasible) {
    return (
      <section className="route-summary route-summary--infeasible">
        <h2 className="route-summary__heading">{heading}</h2>
        <p>No route could be planned{failureReason ? `: ${failureReason}` : "."}</p>
      </section>
    );
  }

  const eta = formatEtaSeconds(etaSeconds);

  return (
    <section className="route-summary">
      <h2 className="route-summary__heading">{heading}</h2>
      <div className="route-summary__stats">
        {distance !== null ? <span>{distance.toFixed(1)} nm</span> : null}
        {eta ? <span>ETA {eta}</span> : null}
        {detourPct !== null && direct !== null ? (
          <span>
            {detourPct.toFixed(0)}% longer than the {direct.toFixed(1)} nm direct line
          </span>
        ) : null}
      </div>
      {avoided.length > 0 ? <div className="route-summary__avoided">Routed around: {avoided.join(", ")}</div> : null}
    </section>
  );
}
