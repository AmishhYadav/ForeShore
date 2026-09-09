/**
 * "Same agent core, different renderer" — PLAN.md Phase 6 calls this out as the
 * strongest architecture claim available and demonstrable in thirty seconds. The panel
 * itself is `GET /api/architecture`'s specialist roster (planner -> specialists ->
 * verdict -> ceiling -> synthesis, restricted tool subset per specialist); the claim it
 * backs is that /boat and /console both call the identical `POST /api/query`, differing
 * only in `surface`. The "Open /boat" link proves it live rather than asserting it:
 * the same architecture panel data is reachable from the other surface too.
 */
import { useState } from "react";
import type { ArchitectureSpecialist } from "@shared/types";

interface ArchitecturePanelProps {
  specialists: ArchitectureSpecialist[];
}

/** The 10 specialists (agents/specialists.py) grouped into the 5 stages a question
 *  actually moves through (agents/orchestrator.py::answer, CLAUDE.md's "planner ->
 *  specialists -> synthesis") — a flat 10-card grid says nothing about collaboration;
 *  this says who hands off to whom and why. Specialists sharing a stage run genuinely
 *  concurrently (`ThreadPoolExecutor`, see orchestrator.py), never in the sequence they
 *  happen to be listed. */
const STAGES: { id: string; title: string; blurb: string; members: string[] }[] = [
  {
    id: "intake",
    title: "1 · Conversational front door",
    blurb: "Sorts every utterance — distress, a real question, smalltalk, out of scope — before anything else runs.",
    members: ["UserInteraction"],
  },
  {
    id: "planning",
    title: "2 · Planning",
    blurb: "Resolves what time window the question is really about and what data it needs before gathering starts.",
    members: ["MarineDataDiscovery", "PlanningAgent"],
  },
  {
    id: "data",
    title: "3 · Data specialists (run concurrently)",
    blurb: "Each restricted to its own tool subset — weather, ocean state, boundaries and routing, gathered in parallel.",
    members: ["WeatherIntelligence", "OceanAnalytics", "GeospatialReasoning", "RoutingAgent"],
  },
  {
    id: "decision",
    title: "4 · Risk & ceiling",
    blurb: "Turns the gathered evidence into one of three verdicts, then a deterministic ceiling check can only downgrade it.",
    members: ["RiskAssessment"],
  },
  {
    id: "output",
    title: "5 · Synthesis & presentation",
    blurb: "Composes the answer and decides what the map/panels should show — never adds a number of its own.",
    members: ["VisualizationAgent", "ReportingAgent"],
  },
];

export default function ArchitecturePanel({ specialists }: ArchitecturePanelProps) {
  const byName = new Map(specialists.map((s) => [s.name, s]));
  const [expanded, setExpanded] = useState<string | null>(null);

  return (
    <div className="architecture-panel">
      <div className="architecture-panel__claim">
        <p>
          <strong>/boat</strong> and <strong>/console</strong> call the exact same{" "}
          <code>POST /api/query</code> endpoint — one agent core, two renderers. Only the{" "}
          <code>surface</code> field differs (<code>"boat"</code> vs <code>"console"</code>), which
          selects language default and copy, never a different reasoning path.
        </p>
        <a className="btn btn--link" href="/boat" target="_blank" rel="noreferrer">
          Open /boat in a new tab to compare →
        </a>
      </div>

      {specialists.length === 0 && <p className="empty-note">No specialist roster returned yet.</p>}

      {specialists.length > 0 && (
        <div className="architecture-flow">
          {STAGES.map((stage, i) => (
            <div className="architecture-flow__step" key={stage.id}>
              <div className="architecture-stage">
                <div className="architecture-stage__title">{stage.title}</div>
                <p className="architecture-stage__blurb">{stage.blurb}</p>
                <div className="architecture-stage__members">
                  {stage.members.map((name) => {
                    const s = byName.get(name);
                    if (!s) return null;
                    const open = expanded === name;
                    return (
                      <article key={name} className="specialist-card">
                        <button
                          type="button"
                          className="specialist-card__head"
                          onClick={() => setExpanded(open ? null : name)}
                          aria-expanded={open}
                        >
                          <span className="specialist-card__name">{s.name}</span>
                          <span className="specialist-card__chevron" aria-hidden="true">
                            {open ? "−" : "+"}
                          </span>
                        </button>
                        <div className="specialist-card__role">{s.role}</div>
                        {open && (
                          <>
                            <div className="specialist-card__ps">PS capability: {s.ps_capability}</div>
                            <div className="specialist-card__tools">
                              {s.tools.length === 0 ? (
                                <span className="specialist-card__no-tools">No tools — reasons over the utterance alone</span>
                              ) : (
                                s.tools.map((t) => (
                                  <span key={t} className="badge badge--outline">
                                    {t}
                                  </span>
                                ))
                              )}
                            </div>
                          </>
                        )}
                      </article>
                    );
                  })}
                </div>
              </div>
              {i < STAGES.length - 1 && (
                <span className="architecture-flow__arrow" aria-hidden="true">
                  ↓
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
