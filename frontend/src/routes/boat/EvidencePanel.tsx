/**
 * "Three sources disagree, one governs" — the project's technical centerpiece
 * (CLAUDE.md). Rows are split into what actually decided the verdict (`governs: true`)
 * and everything else shown for comparison only, and every row carries its own
 * freshness chip so nothing reads as "current" when it is not.
 */
import { freshnessMeta, readEvidenceAcquiredAt, readEvidenceDisplay, readEvidenceResolution } from "./format";

function humanizeVariable(v: string): string {
  return v.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function EvidenceRowCard({ row }: { row: Record<string, unknown> }) {
  const freshness = freshnessMeta(row.freshness as string | undefined);
  const isDerived = Boolean(row.is_derived);
  const governs = Boolean(row.governs);
  const acquiredAt = readEvidenceAcquiredAt(row);
  return (
    <div className={`evidence-row${governs ? " evidence-row--governs" : ""}`}>
      <div className="evidence-row__top">
        <span className="evidence-row__variable">{humanizeVariable(String(row.variable ?? ""))}</span>
        <span className="freshness-chip" style={{ color: freshness.fg, background: freshness.bg }}>
          {freshness.label}
        </span>
      </div>
      <div className="evidence-row__value">{readEvidenceDisplay(row)}</div>
      <div className="evidence-row__source">
        {String(row.source_name ?? "unknown source")}
        {row.authority ? ` · ${String(row.authority)}` : ""}
        {" · "}
        {readEvidenceResolution(row)}
      </div>
      <div className="evidence-row__acquired">
        {acquiredAt ? `acquired ${new Date(acquiredAt).toLocaleString(undefined, { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "short" })}` : "acquisition time unknown"}
      </div>
      <div className="evidence-row__tags">
        {governs ? <span className="tag tag--governs">GOVERNS THIS VERDICT</span> : null}
        {isDerived ? <span className="tag tag--derived">DERIVED — not official</span> : null}
      </div>
    </div>
  );
}

export function EvidencePanel({
  rows,
  heading,
  unsourcedNumbers,
}: {
  rows: Record<string, unknown>[];
  heading: string;
  unsourcedNumbers?: string[];
}) {
  if (!rows || rows.length === 0) {
    return (
      <section className="evidence-panel">
        <h2 className="evidence-panel__heading">{heading}</h2>
        <p className="evidence-panel__empty">No evidence attached to this answer yet.</p>
      </section>
    );
  }

  const governing = rows.filter((r) => r.governs);
  const rest = rows.filter((r) => !r.governs);

  return (
    <section className="evidence-panel">
      <h2 className="evidence-panel__heading">{heading}</h2>

      {governing.length > 0 ? (
        <>
          <div className="evidence-panel__group-label">Governs this verdict</div>
          <div className="evidence-panel__grid">
            {governing.map((r, i) => (
              <EvidenceRowCard key={`gov-${i}`} row={r} />
            ))}
          </div>
        </>
      ) : null}

      {rest.length > 0 ? (
        <>
          <div className="evidence-panel__group-label evidence-panel__group-label--muted">
            Also observed — comparison only, does not govern
          </div>
          <div className="evidence-panel__grid">
            {rest.map((r, i) => (
              <EvidenceRowCard key={`rest-${i}`} row={r} />
            ))}
          </div>
        </>
      ) : null}

      {unsourcedNumbers && unsourcedNumbers.length > 0 ? (
        <div className="evidence-panel__unsourced">
          The answer text omitted {unsourcedNumbers.length} number(s) that could not be traced to a source:{" "}
          {unsourcedNumbers.join(", ")}
        </div>
      ) : null}
    </section>
  );
}
