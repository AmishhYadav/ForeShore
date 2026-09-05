/**
 * The emotional/functional centre of the boat UI (PLAN.md Phase 5). Renders either a
 * live verdict or a cached one (`stale` dims it and swaps in a reconnect notice — an
 * expired cached verdict must never look authoritative). `DO_NOT_ADVISE` is a designed
 * outcome, not an error: same card shape as GO/GO_WITH_CAUTION, just the stop colour
 * and a named human handoff instead of a route.
 *
 * The verdict itself is rendered as a plain-English sentence first ("Safe to go" /
 * "Go with caution" / "Do not go") — the raw enum (`DO_NOT_ADVISE`) is demoted to a
 * small monospace provenance chip in the corner. A non-technical reader should never
 * have to parse the enum to understand the advice.
 */
import type { Handoff, Verdict, VerdictLevel } from "@shared/types";
import { formatClock, plainVerdict, verdictMeta } from "./format";
import "./ask.css";

export interface VerdictCopy {
  headline: string;
  reason: string;
}

function VerdictIcon({ level }: { level: string }) {
  const cls =
    level === "GO"
      ? "verdict-card__icon--go"
      : level === "GO_WITH_CAUTION"
        ? "verdict-card__icon--caution"
        : "verdict-card__icon--stop";
  return <span className={`verdict-card__icon ${cls}`} aria-hidden="true" />;
}

/** Renders a contact number per the demo-directory rule: a real `tel:` link when
 * verified (the default when `contact_verified` is absent), otherwise plain
 * unlinked text plus a "DEMO DIRECTORY" pill — these placeholder numbers must never
 * be dialled. Renders nothing at all when `contact` is null — no placeholder copy,
 * just the VHF line and the Coast Guard handoff below. */
function ContactPieces({
  contact,
  contactLabel,
  verified,
  compact = false,
}: {
  contact: string | null | undefined;
  contactLabel?: string | null;
  verified?: boolean;
  compact?: boolean;
}) {
  if (!contact) return null;
  const linkClass = `handoff-block__contact${compact ? " handoff-block__contact--compact" : ""}`;
  if (verified === false) {
    return (
      <>
        {contactLabel ? <span className="handoff-block__contact-label">{contactLabel}</span> : null}
        <span className={`${linkClass} handoff-block__contact--demo`}>{contact}</span>
        <span className="handoff-block__pill">DEMO DIRECTORY</span>
      </>
    );
  }
  return (
    <>
      {contactLabel ? <span className="handoff-block__contact-label">{contactLabel}</span> : null}
      <a className={linkClass} href={`tel:${contact.replace(/[^\d+]/g, "")}`}>
        {contact}
      </a>
    </>
  );
}

function HandoffBlock({ handoff, label }: { handoff: Handoff; label: string }) {
  const alternates = (handoff.alternates ?? []).slice(0, 2);
  const hasMeta = Boolean(handoff.district) || (handoff.distance_nm !== null && handoff.distance_nm !== undefined);

  return (
    <div className="handoff-block">
      <div className="handoff-block__label">{label}</div>

      <div className="handoff-block__primary">
        <div className="handoff-block__name">{handoff.authority_name}</div>

        {hasMeta ? (
          <div className="handoff-block__meta">
            {handoff.district ? <span className="handoff-block__district">{handoff.district}</span> : null}
            {handoff.distance_nm !== null && handoff.distance_nm !== undefined ? (
              <span className="handoff-block__distance">{handoff.distance_nm.toFixed(1)} nm away</span>
            ) : null}
          </div>
        ) : null}

        {handoff.contact ? (
          <div className="handoff-block__contact-row">
            <ContactPieces contact={handoff.contact} contactLabel={handoff.contact_label} verified={handoff.contact_verified} />
          </div>
        ) : null}

        {handoff.vhf_channel ? <div className="handoff-block__vhf">VHF {handoff.vhf_channel}</div> : null}
      </div>

      {alternates.length > 0 ? (
        <div className="handoff-block__alternates">
          <div className="handoff-block__alternates-title">Other landing centres nearby</div>
          {alternates.map((alt, i) => (
            <div className="handoff-block__alternate" key={i}>
              <span className="handoff-block__alternate-name">{alt.authority_name}</span>
              {alt.distance_nm !== null && alt.distance_nm !== undefined ? (
                <span className="handoff-block__alternate-distance">{alt.distance_nm.toFixed(1)} nm</span>
              ) : null}
              <ContactPieces contact={alt.contact} contactLabel={alt.contact_label} verified={alt.contact_verified} compact />
            </div>
          ))}
        </div>
      ) : null}

      <div className="handoff-block__cg">
        <a className="handoff-block__cg-link" href="tel:1554">
          Indian Coast Guard — 1554
        </a>
      </div>
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
  onSpeak,
  speaking,
}: {
  verdict: Verdict | null;
  copy: VerdictCopy | null;
  labels: Record<string, string>;
  stale?: boolean;
  staleNotice?: string | null;
  emptyHeadline?: string;
  emptyMessage?: string;
  /** When provided, renders a Speak/Stop control on the card footer. Speaking is
   * always an explicit user action — this component never auto-plays audio. */
  onSpeak?: () => void;
  /** True while TTS is playing; drives the Speak/Stop label and icon. */
  speaking?: boolean;
}) {
  if (!verdict || !copy) {
    return (
      <div className="verdict-card verdict-card--empty">
        <div className="verdict-card__empty-headline">{emptyHeadline}</div>
        <div className="verdict-card__empty-message">{emptyMessage}</div>
      </div>
    );
  }

  const meta = verdictMeta(verdict.level);
  const plainLabel = plainVerdict(verdict.level);
  const showBackendHeadline = Boolean(copy.headline) && copy.headline.trim().toLowerCase() !== plainLabel.toLowerCase();

  return (
    <div
      className={`verdict-card${stale ? " verdict-card--stale" : ""}`}
      style={{ background: meta.bg, borderColor: meta.fg }}
    >
      {stale && staleNotice ? <div className="verdict-card__stale-banner">{staleNotice}</div> : null}

      <div className="verdict-card__header">
        <span className="verdict-card__chip">{verdict.level}</span>
      </div>

      <div className="verdict-card__top">
        <VerdictIcon level={verdict.level} />
        <div className="verdict-card__level-group">
          <div className="verdict-card__level" style={{ color: meta.fg }}>
            {plainLabel}
          </div>
          {showBackendHeadline ? <div className="verdict-card__headline">{copy.headline}</div> : null}
        </div>
      </div>

      <div className="verdict-card__reason">{copy.reason}</div>

      {verdict.ceiling_applied && verdict.downgraded_from ? (
        <div className="verdict-card__downgrade">
          <div className="verdict-card__downgrade-title">Made more cautious — advisory ceiling applied</div>
          <div className="verdict-card__downgrade-body">
            {`FORESHORE's own reading of the data was "${plainVerdict(
              verdict.downgraded_from,
            )}". The governing IMD Coastal Bulletin is stricter, so the stricter answer is the one you get: "${plainLabel}". FORESHORE is never allowed to give advice more permissive than IMD.`}
          </div>
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

      {onSpeak ? (
        <div className="verdict-card__footer">
          <button type="button" className="verdict-card__speak" onClick={onSpeak} aria-pressed={Boolean(speaking)}>
            <span className="verdict-card__speak-icon" aria-hidden="true">
              {speaking ? "⏹" : "▶"}
            </span>
            <span className="verdict-card__speak-label">{speaking ? "Stop" : "Speak"}</span>
          </button>
        </div>
      ) : null}
    </div>
  );
}
