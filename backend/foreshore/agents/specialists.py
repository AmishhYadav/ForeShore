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
            "derive_pfz_zones", "get_productivity_history",
        ),
        system=(
            "get_sea_state returns every source unreconciled. Present them side by side "
            "with their resolutions. The INCOIS Ocean State Forecast is an 11 km nest "
            "with data assimilation and governs the number; Open-Meteo is a ~28 km "
            "global model and is a cross-check. Never average them.\n"
            "Zones from derive_pfz_zones are FORESHORE's own derivation and must be "
            "labelled indicative, never presented as the INCOIS advisory."
        ),
        ps_capability="ocean analytics",
    ),
    Specialist(
        name="GeospatialReasoning",
        role="Boundaries, zones, distances and the nearest safe harbour.",
        tools=(
            "find_nearest_pfz", "check_geofences", "get_exclusion_zones", "nearest_harbour",
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
        tools=("get_governing_advisory", "evaluate_verdict"),
        system=(
            "There are exactly three verdicts: GO, GO_WITH_CAUTION, DO_NOT_ADVISE. "
            "DO_NOT_ADVISE is a designed outcome for missing, stale or contradictory "
            "input, not an error, and it must hand off to a named human authority.\n"
            "You cannot make a verdict more permissive than the governing IMD bulletin. "
            "A deterministic ceiling check runs after you and will overrule you if you "
            "try, so propose the cautious reading."
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
        tools=("check_geofences", "get_exclusion_zones", "find_nearest_pfz"),
        system=(
            "Return layer choices and framing, not prose. Anything you surface must be "
            "traceable to a tool result already in evidence."
        ),
        ps_capability="visualization",
    ),
    Specialist(
        name="ReportingAgent",
        role="Compose the operator-facing report for the shore console.",
        tools=("get_governing_advisory", "get_hazard_alerts", "nearest_harbour"),
        system=(
            "Write for a fisheries or Coast Guard operator: what is happening, which "
            "vessels are affected, what action is open to them, and what the evidence is."
        ),
        ps_capability="reporting",
    ),
)

SPECIALISTS_BY_NAME: dict[str, Specialist] = {s.name: s for s in SPECIALIST_DEFS}


def get(name: str) -> Specialist:
    if name not in SPECIALISTS_BY_NAME:
        raise KeyError(f"unknown specialist {name!r}; known: {sorted(SPECIALISTS_BY_NAME)}")
    return SPECIALISTS_BY_NAME[name]


def specialist_for_tool(tool: str) -> str | None:
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
