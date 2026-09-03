/**
 * Side-by-side comparison of two departure-time options (PLAN.md Phase 7 "explore
 * scenarios" — the PS's own wording). The backend only populates `QueryOutcome.scenario`
 * when the question named two explicit departure times; each `ScenarioOption.outcome` is
 * a complete, independent answer with its own `.verdict` and `.payloads.verdict_copy`.
 * This component renders just the compare-at-a-glance slice of each — level + headline +
 * lead — not each option's full evidence panel, route, or map (BoatApp still owns those
 * for the recommended/active outcome). Display-only: no state, no API calls.
 */
import type { ScenarioComparison, ScenarioOption } from "@shared/types";
import { verdictMeta } from "./format";

function ScenarioOptionCard({
  option,
  recommended,
  labels,
}: {
  option: ScenarioOption;
  recommended: boolean;
  labels: Record<string, string>;
}) {
  const verdict = option.outcome.verdict;
  const copy = option.outcome.payloads?.verdict_copy ?? null;
  const meta = verdictMeta(verdict?.level);

  return (
    <div
      className={`scenario-compare__option${recommended ? " scenario-compare__option--recommended" : ""}`}
      style={{ background: meta.bg, borderColor: meta.fg }}
    >
      {recommended ? (
        <div className="scenario-compare__badge">{labels.recommended ?? "Recommended"}</div>
      ) : null}
      <div className="scenario-compare__heading">{option.label}</div>
      {verdict ? (
        <div className="scenario-compare__level" style={{ color: meta.fg }}>
          {verdict.level.replace(/_/g, " ")}
        </div>
      ) : null}
      {copy?.headline ? <div className="scenario-compare__option-headline">{copy.headline}</div> : null}
      {copy?.lead ? <div className="scenario-compare__lead">{copy.lead}</div> : null}
    </div>
  );
}

export function ScenarioCompare({
  scenario,
  labels,
}: {
  scenario: ScenarioComparison;
  labels: Record<string, string>;
}) {
  return (
    <div className="scenario-compare">
      <div className="scenario-compare__grid">
        {scenario.options.map((option, i) => (
          <ScenarioOptionCard
            key={option.when || i}
            option={option}
            recommended={i === scenario.recommended_index}
            labels={labels}
          />
        ))}
      </div>

      {scenario.differences.length > 0 ? (
        <ul className="scenario-compare__differences">
          {scenario.differences.map((d, i) => (
            <li key={i}>{d}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
