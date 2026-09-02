/**
 * The emotional/functional centre of the boat UI (PLAN.md Phase 5). Renders either a
 * live verdict or a cached one (`stale` dims it and swaps in a reconnect notice — an
 * expired cached verdict must never look authoritative). `DO_NOT_ADVISE` is a designed
 * outcome, not an error: same card shape as GO/GO_WITH_CAUTION, just the stop colour
 * and a named human handoff instead of a route.
 */
import type { Handoff, Verdict } from "@shared/types";
import { formatClock, verdictMeta } from "./format";

export interface VerdictCopy {
  headline: string;
  reason: string;
}

function VerdictIcon({ level }: { level: string }) {
  const cls =
    level === "GO" ? "verdict-icon--go" : level === "GO_WITH_CAUTION" ? "verdict-icon--caution" : "verdict-icon--stop";
  return <span className={`verdict-icon ${cls}`} aria-hidden="true" />;
}

function HandoffBlock({ handoff, label }: { handoff: Handoff; label: string }) {
  return (
    <div className="handoff-block">
      <div className="handoff-block__label">{label}</div>
      <div className="handoff-block__name">{handoff.authority_name}</div>
      <div className="handoff-block__row">
        {handoff.contact ? (
          <a className="handoff-block__contact" href={`tel:${handoff.contact.replace(/[^\d+]/g, "")}`}>
            {handoff.contact}
          </a>
        ) : (
          <span className="handoff-block__contact handoff-block__contact--unknown">contact not on file</span>
        )}
        {handoff.distance_nm !== null && handoff.distance_nm !== undefined ? (
          <span className="handoff-block__distance">{handoff.distance_nm.toFixed(1)} nm away</span>
        ) : null}
      </div>
      <div className="handoff-block__cg">Coast Guard: <a href="tel:1554">1554</a></div>
    </div>
  );
}

export function VerdictCard({
  verdict,
  copy,
  labels,
  stale = false,
  staleNotice,
  emptyHeadline = "No advisory yet",
  emptyMessage = "Ask a question — by voice or text — to get a verdict for right now.",
}: {
  verdict: Verdict | null;
  copy: VerdictCopy | null;
  labels: Record<string, string>;
  stale?: boolean;
  staleNotice?: string | null;
  emptyHeadline?: string;
  emptyMessage?: string;
}) {
  if (!verdict || !copy) {
    return (
      <div className="verdict-card verdict-card--empty">
        <div className="verdict-card__headline">{emptyHeadline}</div>
        <div className="verdict-card__reason">{emptyMessage}</div>
      </div>
    );
  }

  const meta = verdictMeta(verdict.level);

  return (
    <div
      className={`verdict-card${stale ? " verdict-card--stale" : ""}`}
      style={{ background: meta.bg, borderColor: meta.fg }}
    >
      {stale && staleNotice ? <div className="verdict-card__stale-banner">{staleNotice}</div> : null}
      <div className="verdict-card__top">
        <VerdictIcon level={verdict.level} />
        <div>
          <div className="verdict-card__level" style={{ color: meta.fg }}>
            {verdict.level.replace(/_/g, " ")}
          </div>
          <div className="verdict-card__headline">{copy.headline}</div>
        </div>
      </div>
      <div className="verdict-card__reason">{copy.reason}</div>

      {verdict.ceiling_applied && verdict.downgraded_from ? (
        <div className="verdict-card__downgrade">
          {labels.downgraded ?? "This advisory was made more cautious"}
          {" "}
          <span className="verdict-card__downgrade-from">(was {verdict.downgraded_from.replace(/_/g, " ")})</span>
        </div>
      ) : null}

      {verdict.level === "DO_NOT_ADVISE" && verdict.handoff ? (
        <HandoffBlock handoff={verdict.handoff} label={labels.handoff ?? "Who to contact"} />
      ) : null}

      <div className="verdict-card__validity">
        {verdict.valid_to ? `Valid until ${formatClock(verdict.valid_to)}` : "No validity window recorded"}
      </div>

      {verdict.reasons.length > 0 ? (
        <details className="verdict-card__details">
          <summary>{labels.why ?? "Why"}</summary>
          <ul>
            {verdict.reasons.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}
