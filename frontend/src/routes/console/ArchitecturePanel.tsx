/**
 * "Same agent core, different renderer" — PLAN.md Phase 6 calls this out as the
 * strongest architecture claim available and demonstrable in thirty seconds. The panel
 * itself is `GET /api/architecture`'s specialist roster (planner -> specialists ->
 * verdict -> ceiling -> synthesis, restricted tool subset per specialist); the claim it
 * backs is that /boat and /console both call the identical `POST /api/query`, differing
 * only in `surface`. The "Open /boat" link proves it live rather than asserting it:
 * the same architecture panel data is reachable from the other surface too.
 */
import type { ArchitectureSpecialist } from "@shared/types";

interface ArchitecturePanelProps {
  specialists: ArchitectureSpecialist[];
}

export default function ArchitecturePanel({ specialists }: ArchitecturePanelProps) {
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
      <div className="architecture-panel__grid">
        {specialists.length === 0 && <p className="empty-note">No specialist roster returned yet.</p>}
        {specialists.map((s) => (
          <article key={s.name} className="specialist-card">
            <div className="specialist-card__name">{s.name}</div>
            <div className="specialist-card__role">{s.role}</div>
            <div className="specialist-card__ps">PS capability: {s.ps_capability}</div>
            <div className="specialist-card__tools">
              {s.tools.map((t) => (
                <span key={t} className="badge badge--outline">
                  {t}
                </span>
              ))}
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}
