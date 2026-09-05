"""The request path, end to end.

``planner.plan`` decides *what* to ask, the specialists ask it with a restricted tool
subset, the deterministic verdict engine decides *whether it is safe*, and
``synthesis.compose`` says it in the user's own language. This module is the only place
those four are joined, and it fixes the order in which they run:

1. **Evidence first.** Every planned tool except ``evaluate_verdict`` runs, and each
   result is pushed onto the evidence bus under this query id.
2. **Specialists reason over that evidence** — only when a real model is configured, and
   only within their own tool subset. A specialist may gather *more* evidence; it cannot
   reach a tool outside its subset (``AgentRuntime.run`` refuses the call and says so).
3. **The verdict runs last**, over everything gathered, through tool 15 — which is a
   wrapper around ``verdict.engine.evaluate``, so the advisory ceiling is applied after
   any model has had its say. The model may make a verdict more cautious and can never
   make it more permissive; that is enforced in the engine, not requested in a prompt.
4. **Synthesis composes the answer** in the detected language and attaches the evidence
   panel.

Two properties follow from that ordering and are worth stating because the whole
submission rests on them:

* With no ``ANTHROPIC_API_KEY``, step 2 is skipped and everything else is unchanged. The
  system still produces the same verdict, the same evidence panel and the same trace —
  only the prose is poorer. The safety decision is in code a reviewer can read.
* ``DO_NOT_ADVISE`` is a designed outcome, not an error state. A failing tool, a missing
  input or an expired bulletin all converge on it, with a named handoff, rather than on a
  traceback or a guess.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Literal, Sequence
from uuid import uuid4

from ..config import RegionConfig, load_region
from ..models import AgentAnswer, Observation, TraceStep, ToolResult, Verdict, is_more_permissive
from ..store.traces import TraceStore, digest, new_step
from ..tools import registry as tool_registry
from ..tools.verdict_tools import clear_evidence, last_outcome, record_evidence
from . import specialists
from .language import detect
from .planner import Plan, plan as build_plan, resolve_scenario_times
from .runtime import AgentRuntime, ScriptedClient
from .synthesis import compose

#: The verdict runs last, always, whatever order the planner put it in.
VERDICT_TOOL = "evaluate_verdict"

#: Variables whose governing reading is highlighted in the evidence panel.
GOVERNING_VARIABLES: tuple[str, ...] = (
    "significant_wave_height",
    "swell_wave_height",
    "wind_speed",
    "current_speed",
)


@dataclass
class Query:
    """One inbound question, from either surface."""

    text: str
    lat: float | None = None
    lon: float | None = None
    when: datetime | None = None
    vessel_class: str | None = None
    heading_deg: float | None = None
    speed_kn: float | None = None
    destination: tuple[float, float] | None = None
    #: Omit to auto-detect and mirror. Never a dropdown — PS bullet 2 is explicit.
    language: str | None = None
    region_id: str | None = None
    query_id: str | None = None
    surface: Literal["boat", "console"] = "boat"
    #: Specialist reasoning turns are the slow part; a console analyst may want them off.
    use_model: bool = True


@dataclass
class QueryOutcome:
    """What the API returns: the answer, plus how it was reached."""

    answer: AgentAnswer
    plan: Plan
    tool_results: list[ToolResult]
    trace: list[TraceStep]
    verdict: Verdict | None
    duration_ms: int
    missing: list[str] = field(default_factory=list)
    specialists_used: list[str] = field(default_factory=list)
    #: Populated only when the utterance itself named two explicit departure times (PLAN.md
    #: Phase 7 item 4) — see :func:`_build_scenario`. ``None`` on every ordinary answer.
    scenario: "ScenarioComparison | None" = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.answer.to_dict(),
            "plan": self.plan.to_dict(),
            "duration_ms": self.duration_ms,
            "missing": self.missing,
            "specialists_used": self.specialists_used,
            "architecture": specialists.architecture(),
            "scenario": self.scenario.to_dict() if self.scenario else None,
        }


@dataclass
class ScenarioOption:
    """One side of a scenario comparison: a full, independent answer for one candidate
    departure time, carrying its own verdict, evidence and trace exactly as if it had
    been asked on its own."""

    label: str
    when: datetime
    outcome: QueryOutcome

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "when": self.when.isoformat(), "outcome": self.outcome.to_dict()}


@dataclass
class ScenarioComparison:
    """"What if I leave at 04:00 instead of 06:00" (PLAN.md Phase 7 item 4), answered as
    a re-plan of the same question at each named time, never as an LLM guess about how
    the two might differ — every difference below is read off the two real verdicts."""

    options: list[ScenarioOption]      # exactly 2, earlier departure first
    differences: list[str]
    #: Index into ``options`` of the more permissive still-actionable choice; the earlier
    #: time wins a tie, since waiting buys nothing when the outcome is identical.
    recommended_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "options": [o.to_dict() for o in self.options],
            "differences": self.differences,
            "recommended_index": self.recommended_index,
        }


def _governing_ids(observations: Sequence[Observation]) -> list[str]:
    """Provenance ids the evidence panel should mark as governing the number.

    Delegated to the verdict engine so the panel and the verdict can never disagree
    about which source won.
    """
    from ..verdict.engine import governing

    ids: list[str] = []
    for variable in GOVERNING_VARIABLES:
        obs = governing(observations, variable)
        if obs is not None:
            ids.append(obs.provenance.provenance_id)
    return ids


def _specialist_order(plan: Plan) -> list[str]:
    """Specialists in plan order, with RiskAssessment last — it holds the verdict."""
    seen: list[str] = []
    for step in plan.steps:
        if step.specialist not in seen:
            seen.append(step.specialist)
    return [s for s in seen if s != "RiskAssessment"] + (
        ["RiskAssessment"] if "RiskAssessment" in seen else []
    )


def _model_available(runtime: AgentRuntime) -> bool:
    return runtime.client.available and not isinstance(runtime.client, ScriptedClient)


def answer(
    query: Query,
    *,
    runtime: AgentRuntime | None = None,
    region: RegionConfig | None = None,
    traces: TraceStore | None = None,
) -> QueryOutcome:
    """Run the whole request path for one question."""
    t0 = time.perf_counter()
    region = region or load_region(query.region_id)
    query_id = query.query_id or str(uuid4())
    language = query.language or detect(query.text, candidates=region.languages)

    # A caller-supplied `when` always means "answer for exactly this instant" — only an
    # inferred-from-text time is ever ambiguous enough to be two candidate times at once.
    # This also stops the recursive sub-answers below (which always set `when` explicitly)
    # from re-triggering scenario detection on their own text.
    if query.when is None:
        scenario_times = resolve_scenario_times(query.text)
        if scenario_times:
            comparison = _build_scenario(
                query, scenario_times, region=region, traces=traces or TraceStore()
            )
            return replace(comparison.options[0].outcome, scenario=comparison)

    runtime = runtime or AgentRuntime(
        registry=tool_registry, traces=traces or TraceStore(), query_id=query_id
    )
    runtime.query_id = query_id

    plan = build_plan(
        query.text,
        query_id=query_id,
        lat=query.lat,
        lon=query.lon,
        when=query.when,
        vessel_class=query.vessel_class,
        heading_deg=query.heading_deg,
        speed_kn=query.speed_kn,
        destination=query.destination,
        region=region,
        language=language,
    )

    steps: list[TraceStep] = []
    results: list[ToolResult] = []
    missing: list[str] = []

    root = runtime._record(  # noqa: SLF001 — same package, and the trace is the point
        new_step(
            query_id,
            "PlanningAgent",
            "plan",
            args={
                "text": query.text,
                "language": language,
                "intents": list(plan.intents),
                "lat": plan.lat,
                "lon": plan.lon,
                "when": plan.when.isoformat(),
                "surface": query.surface,
            },
            result_digest=digest([s.to_dict() for s in plan.steps]),
            why=(
                "Decompose the question into an ordered plan of specialist tool calls. "
                "Each step carries the reason it is in the plan."
            ),
        ),
        steps,
    )

    # -- 1. Evidence ---------------------------------------------------------------------
    # Deterministic, and identical with or without a model. The verdict tool is held back.
    for step in plan.steps:
        if step.tool == VERDICT_TOOL:
            continue
        if step.tool not in runtime.registry:
            missing.append(step.tool)
            runtime._record(  # noqa: SLF001
                new_step(
                    query_id, step.specialist, "error", tool=step.tool, args=step.args,
                    parent_id=root.step_id, why=step.why, ok=False,
                    error=f"tool {step.tool} is not registered in this build",
                ),
                steps,
            )
            continue
        result = runtime.execute_tool(
            step.tool, dict(step.args), agent=step.specialist,
            parent_id=root.step_id, sink=steps, why=step.why,
        )
        results.append(result)
        record_evidence(query_id, result.observations)
        missing.extend(result.missing)

    # -- 2. Specialist reasoning -----------------------------------------------------------
    # Optional by design: the answer must not depend on the model being reachable.
    specialists_used: list[str] = []
    if query.use_model and _model_available(runtime):
        by_specialist = plan.by_specialist()
        for name in _specialist_order(plan):
            if name == "RiskAssessment":
                continue          # its tool is the verdict, and the verdict runs last
            try:
                spec = specialists.get(name)
            except KeyError:
                continue
            gathered = [
                r for r in results
                if any(s.tool == r.tool for s in by_specialist.get(name, []))
            ]
            run = runtime.run(
                name,
                spec.prompt(),
                _specialist_brief(query.text, language, by_specialist.get(name, [])),
                tool_names=list(spec.tools),
                parent_id=root.step_id,
                max_tokens=900,
            )
            specialists_used.append(name)
            steps.extend(run.steps)
            for extra in run.tool_results:
                results.append(extra)
                record_evidence(query_id, extra.observations)
                missing.extend(extra.missing)
            if run.error:
                missing.append(f"{name}:{run.error}")
            del gathered

    # -- 3. Verdict, last ------------------------------------------------------------------
    verdict: Verdict | None = None
    if VERDICT_TOOL in runtime.registry:
        verdict_result = runtime.execute_tool(
            VERDICT_TOOL,
            {
                "lat": plan.lat,
                "lon": plan.lon,
                "vessel_class": plan.vessel_class or query.vessel_class,
                "when": plan.when.isoformat(),
                "evidence_query_id": query_id,
            },
            agent="RiskAssessment",
            parent_id=root.step_id,
            sink=steps,
            why=(
                "Evaluate the deterministic verdict over everything gathered, then apply "
                "the advisory ceiling as a post-check. Runs last so nothing can be more "
                "permissive than the governing IMD bulletin."
            ),
        )
        results.append(verdict_result)
        specialists_used.append("RiskAssessment")
        outcome = last_outcome(query_id)
        if outcome is not None:
            verdict = outcome.verdict
            runtime._record(  # noqa: SLF001
                new_step(
                    query_id, "RiskAssessment", "ceiling",
                    args={
                        "level": verdict.level,
                        "downgraded_from": verdict.downgraded_from,
                        "rules_fired": list(getattr(outcome.ceiling, "rules_fired", [])),
                    },
                    parent_id=root.step_id,
                    result_digest=digest(outcome.ceiling.to_dict()),
                    why=(
                        "The advisory ceiling is a deterministic post-check: FORESHORE "
                        "may be more cautious than the IMD bulletin, never more permissive."
                    ),
                    ok=True,
                ),
                steps,
            )
        missing.extend(verdict_result.missing)
    else:
        missing.append(VERDICT_TOOL)

    # -- 4. Synthesis ----------------------------------------------------------------------
    observations: list[Observation] = []
    for r in results:
        observations.extend(r.observations)

    route = None
    for r in results:
        if r.tool == "plan_route" and r.payload.get("route"):
            route = r.payload["route"]
            break

    composed = compose(
        query_id=query_id,
        question=query.text,
        language=language,
        verdict=verdict,
        tool_results=results,
        trace=steps,
        runtime=runtime if (query.use_model and _model_available(runtime)) else None,
        region=region,
        governing_ids=_governing_ids(observations),
        route=route,
        extras=_extras(results),
        # The console is an analyst surface: its questions are analytical and its answers
        # are allowed to run longer than the boat's four-sentence budget.
        analytical=query.surface == "console",
    )

    clear_evidence(query_id)

    return QueryOutcome(
        answer=composed,
        plan=plan,
        tool_results=results,
        trace=list(composed.trace) or steps,
        verdict=verdict,
        duration_ms=int((time.perf_counter() - t0) * 1000),
        missing=_dedupe(missing),
        specialists_used=_dedupe(specialists_used),
    )


def _specialist_brief(question: str, language: str, plan_steps: Sequence[Any]) -> str:
    """What a specialist is told. Its own plan steps, verbatim, and nothing else."""
    lines = [
        f"The user asked (language {language!r}): {question}",
        "",
        "The planner assigned you these steps, with the reason each is in the plan:",
    ]
    for s in plan_steps:
        lines.append(f"- {s.tool}: {s.why}")
    lines.append("")
    lines.append(
        "The results of those calls are already in your context. Call another tool from "
        "your own subset only if the evidence is genuinely incomplete. Report what the "
        "evidence says, name the source of every number, and say plainly where a value "
        "is missing rather than filling the gap."
    )
    return "\n".join(lines)


def _extras(results: Sequence[ToolResult]) -> list[str]:
    """Sentences the template answer should carry even when no model runs.

    Only tool summaries — every number in them already came from an Observation.
    """
    keep = ("find_nearest_pfz", "check_geofences", "nearest_harbour", "plan_route",
            "get_lightning_nowcast", "get_hazard_alerts", "get_productivity_history",
            "derive_pfz_zones")
    return [r.summary for r in results if r.tool in keep and r.summary]


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for i in items:
        if i:
            seen.setdefault(i, None)
    return list(seen)


def _build_scenario(
    query: Query,
    times: tuple[datetime, datetime],
    *,
    region: RegionConfig,
    traces: TraceStore,
) -> ScenarioComparison:
    """Run the same question once per candidate departure time and diff the two real
    verdicts. Each option is a full, independent :func:`answer` call — same tools, same
    ceiling, same evidence discipline — never an LLM asked to imagine how the answer
    might change. Specialist reasoning is switched off for both (``use_model=False``):
    the comparison is a verdict diff, not two rounds of extra prose, and skipping it
    keeps a scenario question fast and fully deterministic.
    """
    options: list[ScenarioOption] = []
    for t in times:
        sub_query = replace(query, when=t, use_model=False, query_id=None)
        sub_outcome = answer(sub_query, region=region, traces=traces)
        options.append(ScenarioOption(label=f"Leave at {t.strftime('%H:%M')}", when=t, outcome=sub_outcome))

    a, b = options[0], options[1]
    va, vb = a.outcome.verdict, b.outcome.verdict

    differences: list[str] = []
    if va is None or vb is None:
        differences.append("At least one departure time could not be evaluated at all.")
        recommended_index = 0
    elif va.level == vb.level:
        differences.append(f"Verdict is unchanged: {va.level} at both {a.label} and {b.label}.")
        recommended_index = 0
    else:
        differences.append(f"{a.label}: {va.level}. {b.label}: {vb.level}.")
        recommended_index = 0 if is_more_permissive(va.level, vb.level) else 1
        loser = options[1 - recommended_index]
        loser_verdict = loser.outcome.verdict
        if loser_verdict and loser_verdict.reasons:
            differences.append(f"{loser.label} is worse because: {loser_verdict.reasons[-1]}")

    if va and vb and va.ceiling_source and vb.ceiling_source:
        if va.ceiling_source.valid_to != vb.ceiling_source.valid_to:
            differences.append(
                "The governing IMD bulletin's validity window differs between the two "
                "times — one or both may fall outside it."
            )

    return ScenarioComparison(options=options, differences=differences, recommended_index=recommended_index)


__all__ = ["Query", "QueryOutcome", "answer", "VERDICT_TOOL", "GOVERNING_VARIABLES"]
