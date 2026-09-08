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
from typing import Any, Literal, Sequence

from ..config import RegionConfig, load_region
from ..models import utcnow
from .language import detect, normalise
from .specialists import specialist_for_tool

Intent = str

#: What shape of answer the utterance is asking for. Distinct from ``Intent``, which says
#: *which data* is needed; this says *what the answer is*.
#:
#: ``ADVISORY``
#:     The user is asking whether to put to sea. The verdict **is** the answer and leads.
#: ``INFORMATIONAL``
#:     The user is asking for a fact, a position, a count, a history or an explanation.
#:     The safety spine still runs and the verdict is still computed and still shown —
#:     it is never dropped — but it is *context for this position and time*, not the
#:     answer. Leading with "Do not go." when the question was "which vessels are closest
#:     to the IMBL" is a wrong answer, not a cautious one.
AnswerKind = Literal["ADVISORY", "INFORMATIONAL"]

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
        # "hazardous marine conditions" is the problem statement's own phrasing and
        # matched none of the cues above, so the question that asks about hazards most
        # directly was the one that planned no hazard tool. Every surface form is listed
        # because `_cue_hits` word-boundary matches single ASCII words: "hazard" does not
        # match "hazardous".
        "hazard", "hazards", "hazardous", "dangerous", "rough sea", "bad weather",
        "புயல்", "மின்னல்", "எச்சரிக்கை", "puyal", "minnal", "toofan",
    ),
    # "Which regions show high chlorophyll concentration and favourable sea surface
    # temperature?" — a PS bullet that previously matched no cue at all and fell through
    # to the `safety_check` default, so the answer was a verdict and a geofence list.
    # Distinct from `productivity`, which is the *decline* diagnostic over years: this
    # one asks where the good water is now.
    "ocean_productivity": (
        "chlorophyll", "chlorophyl", "chlorophyll concentration", "plankton", "bloom",
        "sea surface temperature", "sst", "water temperature", "warm water",
        "productive waters", "feeding ground", "feeding grounds", "upwelling", "front",
        "favourable", "favorable",
        "பச்சையம்", "கடல்நீர் வெப்பநிலை", "chlorophyll alavu",
    ),
    # "Which fishing zones should be avoided due to hazardous marine conditions or
    # geofencing restrictions?" — the avoid-list question. It used to classify as
    # `fishing_zone` alone and plan the PFZ tools, answering where to *go* when it was
    # asked where not to. Surface forms are listed individually for the same
    # word-boundary reason as above: "restricted" does not match "restrictions".
    "avoid_zone": (
        "avoid", "avoids", "avoided", "avoiding", "keep out", "stay away", "stay clear",
        "geofence", "geofences", "geofencing", "geofenced", "no-go", "off limits",
        "restricted", "restriction", "restrictions", "prohibited", "banned",
        "closed area", "not allowed", "should not enter",
        "தவிர்", "தடை", "thavir", "thadai",
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
    # "When can I go out?", "how long have I got?", "when must I turn back?" — the
    # operational-planning questions verdict/envelope.py exists to answer: the same
    # deterministic verdict, evaluated once per forecast step, rather than once. These
    # are somebody deciding whether and when to put to sea over a window of time, not a
    # neutral fact question, so they are also listed in DECISION_CUES below — see the
    # comment there.
    "operational_planning": (
        "when can i go", "how long", "turn back", "window", "later today",
        "next few days", "best time", "what time should i",
        "எப்போது போகலாம்", "எவ்வளவு நேரம்", "திரும்ப வேண்டும்",
        "eppo pogalaam", "evvalavu neram", "thirumba vendum",
    ),
    # A shore-console question about the tracked fleet rather than about the asker's own
    # boat. Cues are deliberately plural or explicitly qualified: "is my boat safe" is a
    # question about one boat's verdict, not a fleet query, and must not pull the fleet
    # tool into the plan.
    "fleet": (
        "vessels", "boats", "ships", "trawlers", "fleet", "which vessel", "which boat",
        "which ship", "how many boats", "how many vessels", "nearest vessel",
        "closest vessel", "nearest boat", "closest boat", "படகுகள்", "padagugal",
    ),
}

#: Cues that make an utterance a go/no-go question whatever else it contains. Checked
#: before the information interrogatives below and they win outright: "what is the wave
#: height, should I go?" is a decision, and the decision is what the reader needs first.
#: Planning a passage is a decision too — a route question is somebody about to depart.
#: `operational_planning` cues are included for the same reason: "when can I go out?",
#: "how long have I got?" and "when must I turn back?" are the same go/no-go decision
#: stretched over a window of time rather than asked about one instant — the module
#: docstring for `verdict/envelope.py` states this outright ("Everything else in
#: FORESHORE answers 'can I go now' ... the questions are when can I go, how long have I
#: got, and when must I turn back"). Without this, "how long have I got" would fall
#: through to the plain INFORMATION_CUES match on "how long" and answer with a fact
#: about the world instead of leading with the envelope's own verdict-shaped answer.
DECISION_CUES: tuple[str, ...] = (
    INTENT_CUES["safety_check"]
    + INTENT_CUES["route"]
    + INTENT_CUES["scenario"]
    + INTENT_CUES["operational_planning"]
    + ("put to sea", "set out", "head out", "depart", "sail", "venture out", "launch")
)

#: Interrogatives that ask for a fact, a position, a count or an explanation. Single
#: ASCII words here are word-boundary matched by :func:`_cue_hits`, so "list" does not
#: match "listen" and "show" does not match "should".
INFORMATION_CUES: tuple[str, ...] = (
    "what", "which", "where", "why", "who", "list", "show", "explain", "describe",
    "how many", "how much", "how far", "how long", "how deep", "when is", "when does",
    "tell me", "status of", "closest to", "nearest to",
    # Existence questions. "Are there any lightning or cyclone alerts in my area?" is a
    # question about the world, and it was falling through to the ADVISORY default and
    # being answered with "Do not go." before the alerts it asked about.
    "are there", "is there", "any active", "do we have",
    "என்ன", "எங்கே", "ஏன்", "எத்தனை", "எப்போது", "யார்",
    "enna", "enge", "ethanai", "eppo", "yaar",
)

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
    #: What shape of answer the question asked for. Drives presentation only — the
    #: safety spine, the verdict and the ceiling run identically for both kinds.
    answer_kind: AnswerKind = "ADVISORY"
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
            "answer_kind": self.answer_kind,
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


def classify_answer_kind(text: str) -> AnswerKind:
    """Is this a go/no-go question, or a question about the world?

    Deterministic and safety-biased, in this order:

    1. Any decision cue -> ``ADVISORY``. "Should I go", "is it safe", "plan me a route",
       "what if I leave at 04:00" are all somebody deciding whether to put to sea.
    2. Otherwise, any information interrogative -> ``INFORMATIONAL``.
    3. Otherwise -> ``ADVISORY``.

    Rule 3 is the point of the ordering. An utterance we cannot classify — mangled ASR,
    a fragment, a language we do not mirror — gets the safety framing, because a verdict
    offered to someone who did not ask for it costs a sentence, and an answer that buries
    the verdict from someone who did could cost a boat.
    """
    t = normalise(text).lower()
    if any(_cue_hits(t, cue) for cue in DECISION_CUES):
        return "ADVISORY"
    if any(_cue_hits(t, cue) for cue in INFORMATION_CUES):
        return "INFORMATIONAL"
    return "ADVISORY"


#: Generic words -> the geofence class a fleet question is asking about. Class names, not
#: place names: invariant 6 keeps region specifics out of application logic, so the
#: mapping is over the vocabulary of the *classes* and works unchanged in Gujarat.
_FLEET_CLASS_CUES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("cyclone", "hazard", "storm", "exclusion"), "HAZARD_EXCLUSION"),
    (("coral", "seagrass", "mangrove", "sensitive", "habitat"), "ECO_SENSITIVE"),
    (("park", "mpa", "marine national", "reserve", "sanctuary"), "MPA"),
    (("imbl", "boundary", "border", "maritime line"), "IMBL"),
)


def fleet_geofence_class(text: str) -> str | None:
    """Which boundary class a fleet question is measuring against, or ``None`` for all.

    ``"IMBL"`` is the alias tool 17 understands for "both India-Sri Lanka boundary
    classes" — the two remain distinct legal regimes downstream (invariant 5); this only
    says which pair of fences to measure to.
    """
    t = normalise(text).lower()
    for cues, gclass in _FLEET_CLASS_CUES:
        if any(_cue_hits(t, cue) for cue in cues):
            return gclass
    return None


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


def resolve_scenario_times(text: str, now: datetime | None = None) -> tuple[datetime, datetime] | None:
    """Two explicit departure times named in one utterance, earliest first.

    PLAN.md Phase 7 item 4, verbatim: *"what if I leave at 04:00 instead of 06:00" as a
    re-plan over the same evidence with a diffed verdict.* This is the trigger for that
    comparison — deliberately narrow. It fires only when the utterance names **two**
    explicit ``HH:MM`` times, the same shape as the PS's own example, not on "earlier"/
    "later" cues alone (``INTENT_CUES["scenario"]`` still classifies those utterances for
    the trace's ``intents`` list; there just isn't a second instant to compare against,
    so the normal single-answer path is exactly right for them). One time, or none, is
    not a scenario — it is answered the ordinary way.

    Both times are anchored to the same reference day, taken from whichever
    ``TIME_CUES`` phrase appears in the text ("tomorrow", "tonight", ...), defaulting to
    today — the same anchor :func:`resolve_time` itself would use, so a scenario
    comparison and a plain query about the same utterance never disagree about *which
    day* is meant, only which hour.
    """
    now = now or utcnow()
    t = normalise(text).lower()
    matches = list(_HOUR.finditer(t))
    if len(matches) < 2:
        return None
    base = now
    for cue, delta in TIME_CUES:
        if cue in t:
            base = now + delta
            break
    times = [
        base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        for m in matches[:2]
    ]
    times.sort()
    return (times[0], times[1])


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
    answer_kind = classify_answer_kind(text)
    pos = {"lat": lat, "lon": lon}

    steps: list[PlanStep] = [
        _step(tool, why, {**pos, "when": when.isoformat()} if tool != "get_governing_advisory"
              else pos)
        for tool, why in SAFETY_SPINE
    ]

    seen = {s.tool for s in steps}
    #: Notes the intent loop needs to add — currently only the "no destination to route
    #: to" case. Collected here so they reach the Plan alongside the standing notes.
    planning_notes: list[str] = []

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
            # A route question rarely names its destination — "what is the safest route
            # for a fishing vessel?" names none at all. This used to default the
            # destination to the origin, so A* was asked to route a boat to where it
            # already was and returned "0.0 nm over 0 leg(s)": the router had not run,
            # and the answer said it had. Fall back to the region's nearest configured
            # working ground instead, name it in the `why` so the substitution is visible
            # in the trace and both UIs, and plan no route at all when the region
            # declares none — an absent route is honest, a zero-length one is not.
            ground = None if destination else region.default_destination(lat, lon)
            dest = destination or ((ground.lat, ground.lon) if ground else None)
            if dest is None:
                planning_notes.append(
                    "No destination was given and this region configures no working "
                    "grounds, so no route was planned. A route needs somewhere to go."
                )
            else:
                where = (
                    "the destination given"
                    if destination
                    else f"{ground.name}, the nearest working ground this region configures"
                )
                add("plan_route",
                    f"Run A* over the weighted cost field to {where}, so the path is "
                    "optimised against wave, wind, current, depth and boundary proximity "
                    "— never guessed.",
                    {"origin": [lat, lon], "destination": [dest[0], dest[1]],
                     "departure": when.isoformat(), "vessel_class": vessel_class})
                add("get_exclusion_zones",
                    "Collect cyclone polygons, high-wave cells and hard boundaries so the "
                    "router treats them as impassable rather than merely expensive.",
                    {"when": when.isoformat()})
        elif intent == "operational_planning":
            add("get_decision_envelope",
                "Evaluate the deterministic verdict once per forecast step across the "
                "INCOIS OSF horizon, so the answer names when the window closes, when "
                "it next opens, and -- given a return passage time -- when to turn "
                "back, not just whether to go this instant.",
                {**pos, "vessel_class": vessel_class})
        elif intent == "geofence":
            if "fleet" in intents:
                # "Which vessels are closest to the IMBL" cues both intents, but the
                # boundary word is qualifying the *fleet* question, not asking a second
                # one about the asker's own position. Let tool 17 answer it and leave
                # `check_geofences` to the mandatory safety add below, which still runs —
                # it just stops pre-empting the answer the question actually asked for.
                continue
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
        elif intent == "ocean_productivity":
            add("find_productive_waters",
                "Rank the productive water in this area on chlorophyll and sea surface "
                "temperature together — the two signals INCOIS's own fishing-zone method "
                "rests on — and place each zone by bearing and distance from port.",
                {"bbox": list(region.bbox), "when": when.isoformat(),
                 "lat": lat, "lon": lon})
            add("derive_pfz_zones",
                "Derive the indicative zones from the same fields, so the ranking above "
                "can be seen against the fronts it came from. Labelled derived.",
                {"bbox": list(region.bbox), "when": when.isoformat()}, optional=True)
        elif intent == "avoid_zone":
            add("get_exclusion_zones",
                "Collect every area a boat must stay out of right now — cyclone "
                "polygons, high-wave cells and the hard legal boundaries — as one list, "
                "because that list is the answer to this question.",
                {"when": when.isoformat()})
            add("get_hazard_alerts",
                "Pull the active cyclone and warning picture, so 'hazardous' is decided "
                "by today's hazards rather than by what is usually true here.",
                {"bbox": list(region.bbox)})
            add("check_geofences",
                "Measure this position against every boundary class, so the answer names "
                "the restriction that actually applies here rather than reciting all of "
                "them.",
                {**pos, "heading_deg": heading_deg, "speed_kn": speed_kn})
        elif intent == "fleet":
            add("find_vessels_near_boundary",
                "Rank the tracked fleet by distance to the boundary class the question "
                "names — the shore console's own question, answered from vessel "
                "positions rather than from the asker's position.",
                {"geofence_class": fleet_geofence_class(text), "limit": 5})

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
        answer_kind=answer_kind,
        notes=[
            "Plan built deterministically from intent cues; a language model may add "
            "steps but may not remove a safety step.",
            f"Answer kind {answer_kind}: the safety spine and the verdict run either "
            "way; this decides only whether the verdict leads the answer or trails it "
            "as context for the position and time.",
            *planning_notes,
        ],
    )


__all__ = [
    "Plan", "PlanStep", "plan", "classify", "classify_answer_kind", "resolve_time",
    "resolve_scenario_times", "fleet_geofence_class", "AnswerKind",
    "INTENT_CUES", "SAFETY_SPINE", "DECISION_CUES", "INFORMATION_CUES",
]
