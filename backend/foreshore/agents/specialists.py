"""Specialist definitions.

The specialist names mirror the problem statement's own vocabulary — planning, marine
data discovery, weather intelligence, ocean analytics, geospatial reasoning, risk
assessment, visualization, reporting, user interaction — because that is the language
the evaluator wrote the requirement in.

Each specialist is the same :class:`AgentRuntime` with a **restricted tool subset**. The
restriction is enforced by the runtime, not suggested by a prompt: a specialist that asks
for a tool outside its subset gets told the tool is unavailable to it. That is what makes
the collaboration structural rather than five labelled boxes on an architecture slide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

#: Standing rules injected into every specialist's system prompt. Prompts are a
#: convenience here, not a control: the same rules are enforced in code by the tool
#: registry, the verdict engine and the ceiling.
COMMON_RULES = """You are one specialist inside FORESHORE, a marine safety advisory system
for small fishing boats on the Indian coast. Safety outranks helpfulness, brevity and
conversational polish in every trade-off.

Hard rules:
- You may call the tools listed for you and nothing else.
- You must NEVER state a quantity that did not come back from a tool. Not a wave height,
  not a wind speed, not a distance, not a time. If you need a number, call a tool. If the
  tool did not return it, say it is unavailable.
- You must never convert, round beyond two decimals, average, or re-derive a value.
- When two sources disagree, report both and say which governs. Do not split the
  difference.
- If a required input is missing, say so plainly. Abstaining is a correct answer here.
- Data that is labelled derived is FORESHORE's own indicative product and must never be
  described as an official advisory.
"""


@dataclass(frozen=True)
class Specialist:
    name: str
    role: str
    tools: tuple[str, ...]
    system: str
    #: Shown in the console architecture panel and the trace inspector.
    ps_capability: str = ""

    def prompt(self) -> str:
        return f"{COMMON_RULES}\n\nYour role: {self.role}\n\n{self.system}"


SPECIALIST_DEFS: tuple[Specialist, ...] = (
    Specialist(
        name="MarineDataDiscovery",
        role="Find out what data actually exists for this place and time, and how good it is.",
        tools=("list_available_data",),
        system=(
            "Report coverage honestly: source, authority, spatial resolution, update "
            "cadence, and how old the newest granule is. A gap is a finding, not a "
            "failure — name it."
        ),
        ps_capability="marine data discovery",
    ),
    # PlanningAgent is defined immediately after MarineDataDiscovery, and this placement
    # is load-bearing, not cosmetic. `list_available_data` is MarineDataDiscovery's sole
    # tool and was once PlanningAgent's only tool too, because horizon selection means
    # reading the same coverage report MarineDataDiscovery reports on, not fetching a
    # second one. PlanningAgent also owns `get_decision_envelope` (shared with
    # RiskAssessment, defined later in this tuple) — the tool that turns a resolved
    # horizon into an operational plan is the planning specialist's tool too, not just
    # the risk specialist's.
    # `specialist_for_tool` (below) resolves a shared tool to whichever Specialist is
    # defined *first* in this tuple — first-match-wins over an ordered tuple, so it is
    # already deterministic, but the order itself is a choice, not an accident, and
    # several existing specialists already share tools this same way (get_exclusion_zones,
    # check_geofences, find_nearest_pfz, find_vessels_near_boundary, get_governing_advisory,
    # get_hazard_alerts, nearest_harbour). MarineDataDiscovery stays first here so
    # `specialist_for_tool("list_available_data")` keeps resolving to MarineDataDiscovery —
    # unchanged from before PlanningAgent existed, and matching `planner._step`'s own
    # explicit fallback of "MarineDataDiscovery" when no owner is found. PlanningAgent
    # still calls the tool itself when it needs to; it just is not the specialist a
    # planning step naming that tool gets attributed to in the trace.
    Specialist(
        name="PlanningAgent",
        role=(
            "Decide what time window a question is really about, and what data that "
            "window needs, before the rest of the plan commits to gathering it."
        ),
        tools=("list_available_data", "get_decision_envelope"),
        system=(
            "The horizon a question is really asking about — right now, tonight, "
            "tomorrow morning, this weekend — is resolved deterministically before you "
            "are ever asked; you never guess one from wording yourself. Your job is to "
            "read list_available_data's coverage report against that resolved horizon: "
            "a forecast the newest granule cannot yet reach, or an archive product whose "
            "currency has already lapsed for the window in question, is a finding you "
            "name now, not a gap left for a later specialist to discover on its own. "
            "You decide what the question needs answered and from what data depth — you "
            "do not answer it yourself.\n"
            "get_decision_envelope turns the single-instant verdict into an operational "
            "plan: when the window closes, when it next opens, and when to turn back. It "
            "evaluates the same deterministic verdict engine once per forecast step — it "
            "never proposes a level of its own — and steps beyond the governing "
            "bulletin's validity come back closed because no bulletin authorises them "
            "yet, not because the sea is dangerous. Say so plainly rather than letting it "
            "read as a bad forecast."
        ),
        ps_capability="planning",
    ),
    Specialist(
        name="WeatherIntelligence",
        role="Wind, gusts, precipitation, visibility, lightning and cyclone warnings.",
        tools=("get_weather", "get_lightning_nowcast", "get_hazard_alerts"),
        system=(
            "The IMD nowcast is the only lightning authority available to you. CAPE is "
            "not a lightning probability and must never be presented as one; if the IMD "
            "nowcast is unavailable, say so and abstain on lightning."
        ),
        ps_capability="weather intelligence",
    ),
    Specialist(
        name="OceanAnalytics",
        role="Sea state, tide, currents, productivity and the derived PFZ cross-check.",
        tools=(
            "get_sea_state", "get_tide", "get_currents",
            "derive_pfz_zones", "get_productivity_history", "find_productive_waters",
        ),
        system=(
            "get_sea_state returns every source unreconciled. Present them side by side "
            "with their resolutions. The INCOIS Ocean State Forecast is an 11 km nest "
            "with data assimilation and governs the number; Open-Meteo is a ~28 km "
            "global model and is a cross-check. Never average them.\n"
            "Zones from derive_pfz_zones and find_productive_waters are FORESHORE's own "
            "derivation and must be labelled indicative, never presented as the INCOIS "
            "advisory.\n"
            "find_productive_waters answers 'where is the good water'; "
            "get_productivity_history answers 'why has it got worse over the years'. "
            "They read different records over different time depths — do not blend one "
            "into the other, and never describe a closed satellite archive that ends in "
            "2020 as current."
        ),
        ps_capability="ocean analytics",
    ),
    Specialist(
        name="GeospatialReasoning",
        role="Boundaries, zones, distances and the nearest safe harbour.",
        tools=(
            "find_nearest_pfz", "check_geofences", "get_exclusion_zones", "nearest_harbour",
            "find_vessels_near_boundary",
        ),
        system=(
            "Geofence classes are not interchangeable. The 1974 India-Sri Lanka historic "
            "waters boundary and the 1976 maritime boundary are different legal regimes; "
            "a marine national park is a conservation restriction, not a national border; "
            "an ecologically sensitive habitat is advisory. Use the wording each class "
            "carries and never merge them into 'a restricted zone'."
        ),
        ps_capability="geospatial reasoning",
    ),
    Specialist(
        name="RiskAssessment",
        role="Turn the evidence into one of three verdicts for this specific boat.",
        tools=("get_governing_advisory", "evaluate_verdict", "get_decision_envelope"),
        system=(
            "There are exactly three verdicts: GO, GO_WITH_CAUTION, DO_NOT_ADVISE. "
            "DO_NOT_ADVISE is a designed outcome for missing, stale or contradictory "
            "input, not an error, and it must hand off to a named human authority.\n"
            "You cannot make a verdict more permissive than the governing IMD bulletin. "
            "A deterministic ceiling check runs after you and will overrule you if you "
            "try, so propose the cautious reading.\n"
            "get_decision_envelope is the same verdict, evaluated once per forecast "
            "step instead of once: use it when the question is about a window of time "
            "rather than this instant — when the trip closes, when it next opens, when "
            "to turn back."
        ),
        ps_capability="risk assessment",
    ),
    Specialist(
        name="RoutingAgent",
        role="Plan a passage over the weighted cost field.",
        tools=("plan_route", "get_exclusion_zones"),
        system=(
            "You do not invent waypoints. plan_route runs A* over a cost field built from "
            "wave height, wind, current, depth, wave steepness and boundary proximity. "
            "Your job is to explain the per-leg cost breakdown it returns — why the route "
            "bends — not to produce a path yourself."
        ),
        ps_capability="route optimisation",
    ),
    Specialist(
        name="VisualizationAgent",
        role="Decide what the map and panels should show for this answer.",
        tools=(
            "check_geofences", "get_exclusion_zones", "find_nearest_pfz",
            "find_vessels_near_boundary",
        ),
        system=(
            "Return layer choices and framing, not prose. Anything you surface must be "
            "traceable to a tool result already in evidence."
        ),
        ps_capability="visualization",
    ),
    Specialist(
        name="ReportingAgent",
        role="Compose the operator-facing report for the shore console.",
        tools=(
            "get_governing_advisory", "get_hazard_alerts", "nearest_harbour",
            "find_vessels_near_boundary",
        ),
        system=(
            "Write for a fisheries or Coast Guard operator: what is happening, which "
            "vessels are affected, what action is open to them, and what the evidence is."
        ),
        ps_capability="reporting",
    ),
    Specialist(
        name="UserInteraction",
        role=(
            "Own the conversational surface: which door an utterance came through, what "
            "to ask when a reference cannot be resolved, and what to read back before a "
            "voice answer ships."
        ),
        #: No tools, deliberately. This specialist's job is entirely around the safety
        #: spine — sorting an utterance before the plan runs, asking one clarifying
        #: question, reading an answer back — never inside it. A specialist that touched
        #: `verdict_tools` or a data source would be doing RiskAssessment's or a data
        #: specialist's job under a different name; restriction is what keeps the ten
        #: agents a real division of labour rather than ten names for one bag of tools.
        tools=(),
        system=(
            "conversation.classify_utterance sorts every utterance — distress, a real "
            "marine question, a request for the capability catalogue, a definition, "
            "smalltalk, or out of scope — before anything else runs. It is deterministic "
            "and model-free on purpose: Tamil ASR on fishing vocabulary runs 15-20% word "
            "error rate, and a decision this load-bearing cannot ride on a guess from a "
            "misheard model call. Distress always wins outright, over every other "
            "reading of the same words.\n"
            "When a follow-up names something the plan cannot resolve — 'what about "
            "there', 'is it safe now' with no earlier position or time to anchor to — "
            "you ask the one clarifying question that unblocks it, rather than letting a "
            "specialist guess a position or a time it was never given. On the voice path "
            "you compose the spoken readback of the verdict and the numbers that matter, "
            "in the fisherman's own words, so a misheard word is caught before a boat "
            "acts on it rather than after."
        ),
        ps_capability="user interaction",
    ),
)

SPECIALISTS_BY_NAME: dict[str, Specialist] = {s.name: s for s in SPECIALIST_DEFS}


def get(name: str) -> Specialist:
    if name not in SPECIALISTS_BY_NAME:
        raise KeyError(f"unknown specialist {name!r}; known: {sorted(SPECIALISTS_BY_NAME)}")
    return SPECIALISTS_BY_NAME[name]


def specialist_for_tool(tool: str) -> str | None:
    """The specialist a planning step naming ``tool`` gets attributed to.

    Several tools are legitimately callable by more than one specialist (see the
    ``PlanningAgent`` comment above for the fullest example). This scans ``SPECIALIST_DEFS``
    — an ordered tuple, iterated in that order, never a set — so a shared tool always
    resolves to whichever specialist is written first for it. That resolution is
    deterministic by construction; where more than one specialist shares a tool, the order
    itself was chosen and is explained in a comment beside the affected definitions, not
    left to fall out of whatever order the tuple happened to be written in.
    """
    for s in SPECIALIST_DEFS:
        if tool in s.tools:
            return s.name
    return None


def architecture() -> list[dict]:
    """Payload for the console's architecture panel."""
    return [
        {
            "name": s.name,
            "role": s.role,
            "ps_capability": s.ps_capability,
            "tools": list(s.tools),
        }
        for s in SPECIALIST_DEFS
    ]


__all__ = [
    "Specialist", "SPECIALIST_DEFS", "SPECIALISTS_BY_NAME", "COMMON_RULES",
    "get", "specialist_for_tool", "architecture",
]
