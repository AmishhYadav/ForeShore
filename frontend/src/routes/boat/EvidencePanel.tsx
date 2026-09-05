/**
 * "Three sources disagree, one governs" — the project's technical centerpiece
 * (CLAUDE.md). A real answer can carry 30-40 evidence rows; stacking them one-per-card
 * made the tab unreadable. Rows are grouped by `variable` (see `./evidenceGroups.ts`)
 * into categories, laid out side by side in a responsive grid, with the governing
 * reading and any cross-source disagreement surfaced on the card itself — never
 * averaged, never hidden. Every card still carries its own freshness chip so nothing
 * reads as "current" when it is not.
 */
import { useMemo, useState } from "react";
import { freshnessMeta } from "./format";
import { groupEvidence } from "./evidenceGroups";
import type { EvidenceCategoryGroup, EvidenceVariableGroup } from "./evidenceGroups";
import "./evidence.css";

type EvidenceFilter = "all" | "governing" | "disagreements";

const FILTERS: { id: EvidenceFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "governing", label: "Governing only" },
  { id: "disagreements", label: "Disagreements" },
];

function formatAcquired(acquiredAt: string | null): string {
  if (!acquiredAt) return "acquisition time unknown";
  const d = new Date(acquiredAt);
  if (Number.isNaN(d.getTime())) return "acquisition time unknown";
  return d.toLocaleString(undefined, { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "short" });
}

function EvidenceCard({ group }: { group: EvidenceVariableGroup }) {
  const primary = group.readings[0];
  const freshness = freshnessMeta(primary?.freshness);
  const multi = group.readings.length > 1;

  return (
    <div className={`evp-card${group.governs ? " evp-card--governs" : ""}`}>
      <div className="evp-card__header">
        <span className="evp-card__label">{group.label}</span>
        <span className="freshness-chip" style={{ color: freshness.fg, background: freshness.bg }}>
          {freshness.label}
        </span>
      </div>

      <div className="evp-card__primary">{primary ? primary.display : "—"}</div>

      {group.disagreement ? (
        <div className="evp-card__differ">
          <span className="tag tag--differ">SOURCES DIFFER</span>
        </div>
      ) : null}

      <div className="evp-card__readings">
        {group.readings.map((r, i) => (
          <div className="evp-card__reading" key={`${group.variable}-${i}`}>
            <span className="evp-card__reading-text">
              {r.authority} · {r.display} · {r.resolution}
            </span>
            {multi && r.governs ? <span className="tag tag--governs">GOVERNS</span> : null}
            {r.isDerived ? <span className="tag tag--derived">DERIVED — not official</span> : null}
          </div>
        ))}
      </div>

      <div className="evp-card__acquired">
        {primary ? formatAcquired(primary.acquiredAt) : "acquisition time unknown"}
      </div>
    </div>
  );
}

function EvidenceCategorySection({ category }: { category: EvidenceCategoryGroup }) {
  const defaultOpen = category.category === "warnings" || category.category === "sea";
  return (
    <section className="evp-category-section">
      <details className="evp-category" open={defaultOpen}>
        <summary className="evp-category__summary">
          <span className="evp-category__chevron">▶</span>
          {category.label}
          <span className="evp-category__count">{category.groups.length}</span>
        </summary>
        <div className="evp-grid">
          {category.groups.map((g) => (
            <EvidenceCard key={g.variable} group={g} />
          ))}
        </div>
      </details>
    </section>
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
  const [filter, setFilter] = useState<EvidenceFilter>("all");

  const categories = useMemo(() => groupEvidence(rows ?? []), [rows]);

  const filteredCategories = useMemo(() => {
    return categories
      .map((cat) => ({
        ...cat,
        groups: cat.groups.filter((g) => {
          if (filter === "governing") return g.governs;
          if (filter === "disagreements") return g.disagreement;
          return true;
        }),
      }))
      .filter((cat) => cat.groups.length > 0);
  }, [categories, filter]);

  if (!rows || rows.length === 0) {
    return (
      <section className="evp-panel">
        <h2 className="evp-heading">{heading}</h2>
        <p className="evp-empty">No evidence attached to this answer yet.</p>
      </section>
    );
  }

  const totalReadings = rows.length;
  const totalSources = new Set(rows.map((r) => (typeof r.source_name === "string" && r.source_name ? r.source_name : "unknown source"))).size;
  const governingCount = rows.filter((r) => Boolean(r.governs)).length;
  const staleCount = rows.filter((r) => r.freshness === "stale" || r.freshness === "expired").length;

  return (
    <section className="evp-panel">
      <h2 className="evp-heading">{heading}</h2>

      <div className="evp-summary">
        <span className="evp-summary__stat">{totalReadings} readings</span>
        <span>·</span>
        <span className="evp-summary__stat">{totalSources} sources</span>
        <span>·</span>
        <span className="evp-summary__stat">{governingCount} governing this verdict</span>
        {staleCount > 0 ? (
          <>
            <span>·</span>
            <span className="evp-summary__stale">{staleCount} stale/expired</span>
          </>
        ) : null}
      </div>

      <div className="evp-filters">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            className={`evp-filter-chip${filter === f.id ? " evp-filter-chip--active" : ""}`}
            onClick={() => setFilter(f.id)}
            aria-pressed={filter === f.id}
          >
            {f.label}
          </button>
        ))}
      </div>

      {filteredCategories.map((cat) => (
        <EvidenceCategorySection key={cat.category} category={cat} />
      ))}

      {unsourcedNumbers && unsourcedNumbers.length > 0 ? (
        <div className="evp-unsourced">
          The answer text omitted {unsourcedNumbers.length} number(s) that could not be traced to a source:{" "}
          {unsourcedNumbers.join(", ")}
        </div>
      ) : null}
    </section>
  );
}
