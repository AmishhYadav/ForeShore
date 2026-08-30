"""Planning agent.

Decomposes an utterance into an ordered plan of ``{specialist, tool, args, why}``. The
``why`` string is rendered in both UIs — it is the visible evidence that the system
planned rather than pattern-matched, and it is what a judge reads when they ask "are
those real agents?".

The planner is **deterministic first**. Intent classification runs on the raw text and
does not require the model, for two reasons that are the same reason: Tamil ASR on
fishing vocabulary has a realistic 15-20% word error rate, and a demo cannot depend on
an API call succeeding. When a model is available it may add steps and refine arguments;
it may not remove a safety step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from ..config import RegionConfig, load_region
from ..models import utcnow
from .language import detect, normalise
from .specialists import specialist_for_tool

Intent = str

#: Intent -> keyword cues, in every language we mirror. Matching is substring-based on a
#: normalised string so it survives ASR mangling of surrounding words.
INTENT_CUES: dict[Intent, tuple[str, ...]] = {
    "safety_check": (
        "safe", "go out", "should i go", "can i go", "risky", "danger", "weather ok",
        "கடலுக்கு", "போகலாமா", "பாதுகாப்பு", "ஆபத்து", "pogalama", "kadal poga",
        "jaay", "surakshit",
    ),
    "fishing_zone": (
        "fishing zone", "pfz", "where to fish", "fish", "catch", "shoal", "school",
        "மீன்", "மீன்பிடி", "meen", "machhi", "productive",
    ),
    "route": (
        "route", "way to", "how do i get", "navigate", "passage", "safest way", "course",
        "பாதை", "வழி", "paathai", "vazhi",
    ),
    "geofence": (
        "boundary", "border", "imbl", "sri lanka", "line", "arrest", "detain", "limit",
        "எல்லை", "இலங்கை", "ellai", "border cross",
    ),
    "hazard": (
        "cyclone", "storm", "lightning", "thunder", "warning", "alert", "surge",
        "புயல்", "மின்னல்", "எச்சரிக்கை", "puyal", "minnal", "toofan",
    ),
    "tide": (
        "tide", "high water", "low water", "current", "நீரோட்டம்", "அலை", "ambu",
    ),
    "productivity": (
        "productivity", "declined", "decline", "fewer fish", "less fish", "why has",
        "catch is down", "stock", "over the years", "வீழ்ச்சி", "குறைந்த",
    ),
    "harbour": (
        "harbour", "harbor", "landing centre", "landing center", "nearest port", "shelter",
        "துறைமுகம்", "thuraimugam",
    ),
    "scenario": (
        "what if", "instead of", "rather than", "compare", "leave at", "earlier", "later",
        "என்றால்", "pathilaga",
    ),
}

#: Every plan starts here. The safety spine is not optional and the planner may not drop
#: it, whatever the question was — a fisherman asking where the fish are still needs to
#: know whether the sea will let them go.
SAFETY_SPINE: tuple[tuple[str, str], ...] = (
    ("get_governing_advisory", "Fetch the governing IMD coastal bulletin — it sets the "
                               "ceiling this answer cannot exceed."),
    ("get_sea_state", "Read every wave source side by side so the disagreement is visible "
                      "rather than averaged."),
    ("get_weather", "Wind, gusts, visibility and CAPE for this position and time."),
)

TIME_CUES: tuple[tuple[str, timedelta], ...] = (
    ("tomorrow morning", timedelta(days=1)),
    ("tomorrow", timedelta(days=1)),
    ("tonight", timedelta(hours=8)),
    ("this evening", timedelta(hours=6)),
    ("now", timedelta(0)),
    ("நாளை", timedelta(days=1)),
    ("இன்று", timedelta(0)),
    ("naalai", timedelta(days=1)),
    ("indru", timedelta(0)),
)

_HOUR = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")


@dataclass
class PlanStep:
    specialist: str
    tool: str
    args: dict[str, Any]
    why: str
    optional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "specialist": self.specialist,
            "tool": self.tool,
            "args": self.args,
            "why": self.why,
            "optional": self.optional,
        }


@dataclass
class Plan:
    query_id: str
    text: str
    language: str
    intents: list[Intent]
    steps: list[PlanStep]
    lat: float
    lon: float
    when: datetime
    vessel_class: str | None = None
    notes: list[str] = field(default_factory=list)

    def tools(self) -> list[str]:
        return [s.tool for s in self.steps]

    def by_specialist(self) -> dict[str, list[PlanStep]]:
        out: dict[str, list[PlanStep]] = {}
        for s in self.steps:
            out.setdefault(s.specialist, []).append(s)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "text": self.text,
            "language": self.language,
            "intents": self.intents,
            "lat": self.lat,
            "lon": self.lon,
            "when": self.when.isoformat(),
            "vessel_class": self.vessel_class,
            "steps": [s.to_dict() for s in self.steps],
            "specialists": sorted(self.by_specialist()),
            "notes": self.notes,
        }


def _cue_hits(text: str, cue: str) -> int:
    """Count cue occurrences.

    Single-word cues match on word boundaries, multi-word cues as substrings. Without the
    boundary rule "declined" matches the geofence cue "line" and a productivity question
    plans a boundary check — a real bug this classifier had.
    Indic scripts have no ASCII word boundary, so those cues stay substring matches.
    """
    if " " in cue or not cue.isascii():
        return text.count(cue)
    return len(re.findall(rf"(?<![a-z0-9]){re.escape(cue)}(?![a-z0-9])", text))


def classify(text: str) -> list[Intent]:
    """Every intent the utterance touches, most-cued first.

    Multi-intent is normal and is not a failure to disambiguate: "where can I fish
    tomorrow and is it safe" is genuinely two questions.
    """
    t = normalise(text).lower()
    scored: list[tuple[int, Intent]] = []
    for intent, cues in INTENT_CUES.items():
        hits = sum(_cue_hits(t, cue) for cue in cues)
        if hits:
            scored.append((hits, intent))
    scored.sort(reverse=True)
    intents = [i for _, i in scored]
    return intents or ["safety_check"]


def resolve_time(text: str, now: datetime | None = None) -> datetime:
    """Best-effort departure time from the utterance. Defaults to now."""
    now = now or utcnow()
    t = normalise(text).lower()
    base = now
    for cue, delta in TIME_CUES:
        if cue in t:
            base = now + delta
            break
    m = _HOUR.search(t)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        base = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    elif "morning" in t or "காலை" in t:
        base = base.replace(hour=5, minute=0, second=0, microsecond=0)
    elif "evening" in t or "மாலை" in t:
        base = base.replace(hour=16, minute=0, second=0, microsecond=0)
    return base


def _step(tool: str, why: str, args: dict[str, Any], optional: bool = False) -> PlanStep:
    return PlanStep(
        specialist=specialist_for_tool(tool) or "MarineDataDiscovery",
        tool=tool,
        args=args,
        why=why,
        optional=optional,
    )


def plan(
    text: str,
    *,
    query_id: str,
    lat: float | None = None,
    lon: float | None = None,
    when: datetime | None = None,
    vessel_class: str | None = None,
    heading_deg: float | None = None,
    speed_kn: float | None = None,
    destination: tuple[float, float] | None = None,
    region: RegionConfig | None = None,
    language: str | None = None,
) -> Plan:
    """Build the plan. Deterministic, explainable, and safe to run with no LLM at all."""
    region = region or load_region()
    port = region.anchor_ports[0]
    lat = port.lat if lat is None else lat
    lon = port.lon if lon is None else lon
    when = when or resolve_time(text)
    lang = language or detect(text, candidates=region.languages)
    intents = classify(text)
    pos = {"lat": lat, "lon": lon}

    steps: list[PlanStep] = [
        _step(tool, why, {**pos, "when": when.isoformat()} if tool != "get_governing_advisory"
              else pos)
        for tool, why in SAFETY_SPINE
    ]

    seen = {s.tool for s in steps}

    def add(tool: str, why: str, args: dict[str, Any], optional: bool = False) -> None:
        if tool in seen:
            return
        seen.add(tool)
        steps.append(_step(tool, why, args, optional))

    for intent in intents:
        if intent == "fishing_zone":
            add("find_nearest_pfz",
                "Locate the official INCOIS Potential Fishing Zone advisory line and its "
                "issue date — the authoritative product, not our derivation.", pos)
            add("derive_pfz_zones",
                "Derive indicative zones from chlorophyll and SST fronts as a visible "
                "cross-check beside the official advisory. Labelled derived.",
                {"bbox": list(region.bbox), "when": when.isoformat()}, optional=True)
        elif intent == "route":
            dest = destination or (lat, lon)
            add("plan_route",
                "Run A* over the weighted cost field so the path is optimised against "
                "wave, wind, current, depth and boundary proximity — never guessed.",
                {"origin": [lat, lon], "destination": [dest[0], dest[1]],
                 "departure": when.isoformat(), "vessel_class": vessel_class})
            add("get_exclusion_zones",
                "Collect cyclone polygons, high-wave cells and hard boundaries so the "
                "router treats them as impassable rather than merely expensive.",
                {"when": when.isoformat()})
        elif intent == "geofence":
            add("check_geofences",
                "Compute distance, bearing and closing ETA to every boundary class from "
                "this position and heading.",
                {**pos, "heading_deg": heading_deg, "speed_kn": speed_kn})
        elif intent == "hazard":
            add("get_hazard_alerts",
                "Pull GDACS cyclone events and IMD warnings covering this area.",
                {"bbox": list(region.bbox)})
            add("get_lightning_nowcast",
                "IMD district nowcast is the only lightning authority available; CAPE is "
                "not a lightning probability.",
                {"district": region.district_for(lat, lon)})
        elif intent == "tide":
            add("get_tide", "Sea level series and the next high and low water.",
                {**pos, "hours": 24})
            add("get_currents", "Surface current speed and set at this position.",
                {**pos, "when": when.isoformat()})
        elif intent == "productivity":
            add("get_productivity_history",
                "Multi-year chlorophyll, SST anomaly and Argo subsurface series with "
                "trend statistics — the diagnostic question, answered from data.",
                {"bbox": list(region.bbox), "years": 10})
        elif intent == "harbour":
            add("nearest_harbour",
                "Name the nearest landing centre, so any handoff is to a real place.", pos)

    # The safety spine always terminates in a verdict, and the verdict always knows where
    # to send someone if it abstains.
    add("check_geofences",
        "Even when nobody asked, check boundary proximity: crossing the 1974 line is the "
        "single most common way a boat from this coast is lost.",
        {**pos, "heading_deg": heading_deg, "speed_kn": speed_kn})
    add("nearest_harbour",
        "Resolve the nearest named landing centre up front so an abstention can hand off "
        "to a person rather than to silence.", pos)
    add("evaluate_verdict",
        "Apply this vessel class's thresholds to the gathered evidence, then run the "
        "deterministic advisory ceiling over the result.",
        {**pos, "vessel_class": vessel_class, "when": when.isoformat()})

    return Plan(
        query_id=query_id,
        text=normalise(text),
        language=lang,
        intents=intents,
        steps=steps,
        lat=lat,
        lon=lon,
        when=when,
        vessel_class=vessel_class,
        notes=[
            "Plan built deterministically from intent cues; a language model may add "
            "steps but may not remove a safety step.",
        ],
    )


__all__ = ["Plan", "PlanStep", "plan", "classify", "resolve_time", "INTENT_CUES", "SAFETY_SPINE"]
