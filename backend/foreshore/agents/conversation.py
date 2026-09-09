"""The conversational front door.

Every utterance entering FORESHORE used to become a marine question. ``planner.classify``
ends ``return intents or ["safety_check"]``, so "hello", "what can you do?" and "my engine
failed" all planned the full safety spine, spent five model calls and ~15 seconds, and
answered with a sea-state verdict. A verdict offered to someone who said "hello" is not
cautious, it is wrong — and a boat with a dead engine got a wave height instead of the
Coast Guard.

This module is the door in front of that pipeline. It is **deterministic and model-free**,
like every other classifier here (`planner.classify`, `planner.classify_answer_kind`), for
the same two reasons: Tamil ASR on fishing vocabulary has a 15-20% word error rate, and a
demo cannot depend on an API call.

It decides only *which door an utterance came through*. It never decides a verdict, never
relaxes the ceiling, and never touches the safety spine — an ``OPERATIONAL`` utterance is
handed to the existing planner completely unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Literal

from ..config import (
    GlossaryConfig,
    GlossaryTerm,
    RegionConfig,
    load_glossary,
    load_region,
    load_vessels,
)
from ..models import Handoff, Observation, Provenance, utcnow
from .language import normalise
from .planner import INTENT_CUES, _cue_hits
from .runtime import ScriptedClient, make_client

#: Which door an utterance came through.
#:
#: ``OPERATIONAL``
#:     A real marine question. Handed to `planner.plan` untouched — the existing pipeline,
#:     the safety spine, the ceiling, all of it. This is the only kind that runs tools.
#: ``DISTRESS``
#:     Someone is in trouble now. Short-circuits to a named handoff on the fastest path in
#:     the system, with no model call and no network dependency.
#: ``CAPABILITY``
#:     "What can you do", "who are you". Answered from the tool registry and the
#:     specialist catalogue, so it can never drift from what is actually registered.
#: ``CONCEPT``
#:     "What is a PFZ", "what does Douglas 4 mean". Answered from the glossary.
#: ``SMALLTALK``
#:     A greeting or thanks, with no question attached.
#: ``OUT_OF_SCOPE``
#:     Prawn prices, jokes, the weather in Delhi. Says what it does not do and names what
#:     it does. Never guesses at a marine reading of an unrelated question.
UtteranceKind = Literal[
    "OPERATIONAL", "DISTRESS", "CAPABILITY", "CONCEPT", "SMALLTALK", "OUT_OF_SCOPE"
]

# --------------------------------------------------------------------------------------
# Cues
# --------------------------------------------------------------------------------------

#: Someone is in trouble *now*. Biased towards recall on purpose: a false positive costs a
#: fisherman one screen naming the Coast Guard, which is never harmful; a false negative
#: answers a sinking boat with a wave height. When in doubt this set gets the benefit.
#:
#: Multi-word cues match as substrings and single ASCII words on boundaries
#: (`planner._cue_hits`), which is what keeps "help" out of here — a bare "help" is
#: overwhelmingly someone asking what the tool does, so it lives in CAPABILITY_CUES and
#: only the explicitly-asking-for-rescue phrasings are distress.
DISTRESS_CUES: tuple[str, ...] = (
    "sos", "mayday", "emergency", "distress",
    "send help", "need help", "help me", "we need help", "rescue",
    "engine failed", "engine failure", "engine died", "engine dead", "engine stopped",
    "engine trouble", "engine not working", "engine won't start", "engine wont start",
    "breakdown", "broken down",
    "taking water", "taking on water", "sinking", "we are sinking", "capsized", "capsize",
    "man overboard", "fell overboard", "overboard",
    "adrift", "we are drifting", "boat is drifting", "stranded", "stuck at sea",
    "fire on board", "collision", "collided", "hit a rock",
    "injured", "bleeding", "unconscious", "heart attack", "medical emergency",
    "boat missing", "missing at sea", "has not returned", "not returned",
    "lost at sea", "we are lost",
    # Tamil script + romanised. Indic cues are substring-matched (no ASCII boundaries).
    "உதவி", "அவசரம்", "விபத்து", "மூழ்கு", "காப்பாற்று",
    "udhavi", "uthavi", "avasaram", "vibathu", "kappathu",
)

#: Marine nouns that make an utterance operational even when no `INTENT_CUES` entry fires.
#: This set exists because `INTENT_CUES` has no cue for "wave", "swell" or "forecast" —
#: "what is the wave height today" reaches the planner today only via its
#: `safety_check` default. Without this set, adding a front door would have re-routed
#: those to CONCEPT or OUT_OF_SCOPE, which would be a regression, not a feature.
MARINE_NOUNS: tuple[str, ...] = (
    "wave", "waves", "swell", "sea state", "sea condition", "surf",
    "wind", "gust", "gusts", "breeze", "squall",
    "weather", "forecast", "rain", "visibility", "fog",
    "temperature", "sst", "depth", "bathymetry", "shallow", "deep", "how deep", "water",
    "sea", "ocean", "coast", "offshore", "nautical", "knots", "metres of water",
    "அலை", "காற்று", "கடல்", "வானிலை",
)

#: "What can you do." Answered from the registry, never from a hand-written list.
CAPABILITY_CUES: tuple[str, ...] = (
    "what can you do", "what do you do", "what can i ask", "what can you tell me",
    "who are you", "what are you", "what is foreshore", "how do you work",
    "who made you", "who built you", "who created you", "who developed you",
    "how does this work", "how can you help", "what data do you have",
    "what sources", "capabilities", "commands", "help",
    "நீ யார்", "என்ன செய்வாய்", "எப்படி வேலை",
)

#: Greetings and thanks, with nothing else attached.
SMALLTALK_CUES: tuple[str, ...] = (
    "hello", "hi", "hey", "good morning", "good afternoon", "good evening", "good night",
    "thanks", "thank you", "thankyou", "ok thanks", "bye", "goodbye", "see you",
    "வணக்கம்", "நன்றி", "vanakkam", "nandri",
)

#: Frames that make an utterance a request for a *definition* rather than a *reading*.
DEFINITIONAL_FRAMES: tuple[str, ...] = (
    "what is", "what are", "what does", "what's", "whats",
    "meaning of", "means", "mean by", "explain", "define", "definition of",
    "tell me about", "what do you mean",
    "என்றால் என்ன", "அர்த்தம்",
)

#: Words that pin an utterance to *this* place or *this* time, which is what separates
#: "what is a potential fishing zone?" (a definition) from "what is the potential fishing
#: zone today?" (a reading). Their presence forbids the CONCEPT branch.
DEIXIS_CUES: tuple[str, ...] = (
    "today", "tonight", "now", "right now", "tomorrow", "this morning", "this evening",
    "this afternoon", "currently", "at the moment",
    "here", "near me", "nearest", "closest", "my location", "my position", "this area",
    "around me", "where i am", "my boat",
    "இன்று", "இப்போது", "நாளை", "இங்கே",
)


def _hits(text: str, cues: Iterable[str]) -> int:
    return sum(_cue_hits(text, cue) for cue in cues)


#: Flattened `INTENT_CUES` — every cue that means "this is a marine question for the
#: planner". Built once at import; `INTENT_CUES` is a module-level constant and never
#: mutated at runtime.
OPERATIONAL_CUES: tuple[str, ...] = tuple(
    {cue for cues in INTENT_CUES.values() for cue in cues} | set(MARINE_NOUNS)
)


def is_definitional(text: str, glossary_terms: Iterable[str]) -> bool:
    """True when the utterance asks what something *means*, not what it *is right now*.

    Three conditions, all required: a definitional frame, a known glossary term, and no
    deixis. The third is what keeps "what is the wave height today" operational while
    "what is significant wave height" is a definition — a distinction a human reader makes
    from exactly the same signal.
    """
    t = normalise(text).lower()
    if not _hits(t, DEFINITIONAL_FRAMES):
        return False
    if _hits(t, DEIXIS_CUES):
        return False
    return _hits(t, [term.lower() for term in glossary_terms]) > 0


def classify_utterance(
    text: str, *, glossary_terms: Iterable[str] | None = None
) -> UtteranceKind:
    """Which door this utterance came through.

    The order below **is** the safety argument, and it is not negotiable:

    1. **Distress wins outright**, over everything, including an operational cue. "Engine
       failed, is it safe to go back?" is not a sea-state question.
    2. **A definitional question about a known glossary term** — with no deixis — is a
       CONCEPT. This sits above OPERATIONAL only because "what is a PFZ" contains the
       operational cue "pfz"; the deixis guard in `is_definitional` is what stops it from
       swallowing "where is the nearest PFZ today".
    3. **Any operational cue** makes it OPERATIONAL. This sits above capability and
       smalltalk so that "hello, is it safe to go out?" is a safety question with a
       greeting attached, not a greeting.
    4. Capability. 5. Smalltalk. 6. Otherwise, OUT_OF_SCOPE.

    Rule 6 deliberately inverts today's behaviour. `planner.classify` currently defaults an
    unclassifiable utterance to ``safety_check``, which invents a verdict for a question
    nobody asked. Saying "I cannot answer that, here is what I can answer" is the honest
    outcome — and no safety is lost, because rules 1 and 3 already caught everything that
    was actually about the sea.

    `planner.classify_answer_kind`'s own safety-biased default is untouched and still
    governs every ``OPERATIONAL`` utterance.
    """
    t = normalise(text).lower()

    if _hits(t, DISTRESS_CUES):
        return "DISTRESS"

    if glossary_terms and is_definitional(text, glossary_terms):
        return "CONCEPT"

    if _hits(t, OPERATIONAL_CUES):
        return "OPERATIONAL"

    if _hits(t, CAPABILITY_CUES):
        return "CAPABILITY"

    if _hits(t, SMALLTALK_CUES):
        return "SMALLTALK"

    return "OUT_OF_SCOPE"


#: Kinds that skip the tool pipeline entirely. `orchestrator.answer` branches on this
#: rather than on the individual kinds, so adding a seventh kind later cannot accidentally
#: route it through the full planner.
SHORT_CIRCUIT_KINDS: frozenset[str] = frozenset(
    {"DISTRESS", "CAPABILITY", "CONCEPT", "SMALLTALK", "OUT_OF_SCOPE"}
)


__all__ = [
    "UtteranceKind",
    "classify_utterance",
    "is_definitional",
    "SHORT_CIRCUIT_KINDS",
    "DISTRESS_CUES",
    "CAPABILITY_CUES",
    "SMALLTALK_CUES",
    "MARINE_NOUNS",
    "OPERATIONAL_CUES",
    "DEFINITIONAL_FRAMES",
    "DEIXIS_CUES",
]


# ========================================================================================
# Handlers — everything below is appended to the fixed contract above, not part of it.
# ========================================================================================
#
# Five doors, five handlers, one reply shape. Every handler is deterministic and
# model-free, for the same reason the classifier above is: a decision this close to the
# front door cannot depend on an API call succeeding, and none of these five kinds is a
# marine question a specialist needs to reason over — DISTRESS is a lookup and a named
# handoff, CAPABILITY and CONCEPT are generated straight from what is actually
# registered/configured, and SMALLTALK/OUT_OF_SCOPE are fixed framing. An OPERATIONAL
# utterance never reaches this section; it is handed to `planner.plan` untouched.


@dataclass(frozen=True)
class ConversationReply:
    """What a short-circuit handler returns. Shaped like the pieces
    `agents/orchestrator.py` needs to build an `AgentAnswer` and a `QueryOutcome` without
    running `synthesis.compose` — `text` is the whole answer, `observations` are every
    sourced number in it (invariant 3), `payload` is extra structure for the trace/UI,
    and `handoff` is set only when the reply hands off to a named human authority."""

    text: str
    observations: tuple[Observation, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    handoff: "Handoff | None" = None


# --------------------------------------------------------------------------------------
# Language gate — every handler honours it independently of what its caller passes, the
# same rule tools/geofence_tools.py:115 `_summary_language` applies. A handler tested in
# isolation (as backend/tests/test_conversation.py does) must not be able to leak a
# language this region has not put on `surface_languages`, whether or not the caller
# remembered to gate it first.
# --------------------------------------------------------------------------------------


def _surface_language(language: str | None, region: RegionConfig) -> str:
    surface = region.surface_languages or ("en",)
    if language and language in surface:
        return language
    return surface[0]


# --------------------------------------------------------------------------------------
# DISTRESS — the most important path. No model call, no network dependency: the nearest
# landing centre is resolved from the local file-backed VectorStore over the
# already-committed data/static/landing_centres.geojson, never from IncoisWFS.
# --------------------------------------------------------------------------------------

#: Sanity bound on the local nearest-centre lookup. `VectorStore.nearest` with no
#: `max_nm` always returns the closest feature in the layer, however far away it
#: actually is — for a position nowhere near this region's chart that would be a
#: landing centre thousands of nautical miles off, presented as a real handoff. Past
#: this bound the match is not a usable handoff and the regional Coast Guard line (via
#: `regional_handoff`) is the honest answer instead. Generous on purpose — well beyond
#: any configured vessel class's range — so it only ever excludes a genuinely
#: out-of-region position, never a real nearby centre.
_MAX_HANDOFF_DISTANCE_NM = 150.0

#: 16-point compass words, so the reading is spoken the way a person reads a compass —
#: the exact bearing stays on the Observation's qualifiers for the map. Duplicated
#: (rather than imported) from tools/productive_waters.py's own private `_compass_point`
#: on purpose: that name is not exported, and this module must not reach into another
#: tool's private helpers to stay decoupled from it.
_COMPASS_POINTS: tuple[str, ...] = (
    "north", "north-northeast", "northeast", "east-northeast",
    "east", "east-southeast", "southeast", "south-southeast",
    "south", "south-southwest", "southwest", "west-southwest",
    "west", "west-northwest", "northwest", "north-northwest",
)


def _compass_point(bearing: float | None) -> str | None:
    if bearing is None:
        return None
    idx = int(((bearing % 360.0) + 11.25) // 22.5) % 16
    return _COMPASS_POINTS[idx]


def _contact_for_centre(name: str | None, district: str | None) -> dict[str, Any]:
    """Directory lookup for one landing centre, config-only (no network) — the same
    shape as `tools/harbour.py::_contact_fields`, kept as a small local copy rather than
    an import so this module never reaches into that tool's private API."""
    from ..config import load_contact_directory

    entry, kind = load_contact_directory().for_centre(name, district)
    if entry is None:
        return {
            "contact": None, "contact_label": None, "vhf_channel": None,
            "contact_verified": False, "authority_name": name,
        }
    if kind == "centre":
        return {
            "contact": entry.contact, "contact_label": entry.contact_label,
            "vhf_channel": entry.vhf_channel, "contact_verified": entry.verified,
            "authority_name": entry.authority_name,
        }
    label = (
        f"{entry.authority_name} — {entry.contact_label}"
        if entry.contact_label else entry.authority_name
    )
    return {
        "contact": entry.contact, "contact_label": label,
        "vhf_channel": entry.vhf_channel, "contact_verified": entry.verified,
        "authority_name": name,
    }


def distress_reply(
    lat: float,
    lon: float,
    *,
    region: RegionConfig | None = None,
    language: str = "en",
) -> ConversationReply:
    """Someone is in trouble now. Answers with the network down.

    Resolves the nearest landing centre entirely against the local, file-backed
    `store.vectors.VectorStore` over `data/static/landing_centres.geojson` — never
    `tools/harbour.py::nearest_harbour`, which reaches `IncoisWFS` over the network.
    Falls back to `verdict/ceiling.py::regional_handoff` (the regional Coast Guard line)
    when the local layer resolves nothing near this position. Always returns a
    `Handoff`; never calls a model.
    """
    from ..store.vectors import VectorStore
    from ..verdict.ceiling import regional_handoff

    region = region or load_region()
    _surface_language(language, region)  # gated for symmetry with the other handlers;
    # every branch below is English regardless — this is the one reply where a wrong
    # word costs more than an untranslated one, so it stays in the language the operator
    # who wrote it could actually verify.

    when = utcnow()
    observations: list[Observation] = []
    handoff: Handoff

    try:
        results = VectorStore().nearest(
            "landing_centres", lat, lon, n=1, max_nm=_MAX_HANDOFF_DISTANCE_NM
        )
    except Exception:  # noqa: BLE001 — a broken local store must still hand off
        results = []

    if results:
        nr = results[0]
        props = nr.feature.properties
        name = str(props.get("LC_NAME") or props.get("name") or "the nearest landing centre").strip()
        district = props.get("DIST_NAME") or props.get("district")
        distance = round(nr.distance_nm, 2)
        compass = _compass_point(nr.bearing_deg)

        prov = Provenance(
            source_id="landing_centres_local",
            source_name="INCOIS PFZ_LandingCentres (local snapshot)",
            authority="INCOIS",
            url="https://incois.gov.in/geoserver/PFZ_LandingCentres/ows",
            acquired_at=nr.feature.acquired_at,
            issued_at=nr.feature.acquired_at,
            is_derived=False,
            notes=(
                "Resolved from the local file-backed vector store over "
                "data/static/landing_centres.geojson — no network call, so this handoff "
                "still resolves with the network down."
            ),
        )
        observations.append(Observation(
            variable="landing_centre_distance",
            value=distance,
            unit="nm",
            lat=lat,
            lon=lon,
            valid_time=prov.acquired_at,
            provenance=prov,
            qualifiers={
                "name": name,
                "district": district,
                "bearing_deg": nr.bearing_deg,
                "compass": compass,
                "closest_lat": nr.closest_lat,
                "closest_lon": nr.closest_lon,
            },
        ))

        fields = _contact_for_centre(name, district)
        handoff = Handoff(
            reason=(
                "Distress utterance — immediate handoff to the nearest named landing "
                "centre and the Coast Guard."
            ),
            authority_name=fields["authority_name"] or name,
            authority_type="landing_centre",
            contact=fields["contact"],
            contact_label=fields["contact_label"],
            contact_verified=fields["contact_verified"],
            vhf_channel=fields["vhf_channel"] or "Ch 16",
            district=district,
            lat=nr.closest_lat,
            lon=nr.closest_lon,
            distance_nm=distance,
            provenance=prov,
        )
        where = f"{compass} of you" if compass else "at your position"
        district_bit = f", {district} district" if district else ""
        centre_line = f"Nearest named landing centre: {name}{district_bit}, about {distance:.1f} nm {where}."
    else:
        handoff = regional_handoff(
            "Distress utterance — no local landing-centre match; regional Coast Guard handoff.",
            region,
        )
        centre_line = (
            "No named landing centre could be resolved for this position from the local "
            "chart, so the Coast Guard is the handoff."
        )

    cg_line = "Indian Coast Guard: 1554 (Maritime distress), VHF channel 16."
    pos_line = f"Your reported position, for radio relay: {lat:.3f}°N, {lon:.3f}°E."
    text = " ".join([
        "This sounds like an emergency.",
        centre_line,
        cg_line,
        pos_line,
        "Call now, or hail on VHF channel 16, and give this position.",
    ])

    return ConversationReply(
        text=text,
        observations=tuple(observations),
        payload={"handoff": handoff.to_dict()},
        handoff=handoff,
    )


# --------------------------------------------------------------------------------------
# CAPABILITY — generated at call time from the tool registry and the specialist
# catalogue, so it can never drift from what is actually registered. Never calls
# `tools/discovery.list_available_data`, which probes every adapter's `.health()` over
# the network and would make the fastest question in the system one of the slowest.
# --------------------------------------------------------------------------------------


def capability_reply(
    language: str = "en", *, region: RegionConfig | None = None
) -> ConversationReply:
    from . import specialists as specialists_module
    from ..tools import registry as tool_registry

    region = region or load_region()
    _surface_language(language, region)

    total_tools = len(tool_registry.all())
    lines = [
        f"FORESHORE is a marine safety advisory system for {region.display_name_en}, "
        "for small-boat fishermen and the shore-side fisheries and Coast Guard console. "
        "Every answer is backed by named, dated sources and settles on one of three "
        "advisory levels: go, go with caution, or do not advise.",
        "",
        f"It works through {len(specialists_module.SPECIALIST_DEFS)} specialist agents, "
        f"each restricted to its own set of tools ({total_tools} tools in total):",
    ]
    for spec in specialists_module.SPECIALIST_DEFS:
        n = len(tool_registry.for_specialist(spec.name))
        tool_bit = f"{n} tool{'s' if n != 1 else ''}" if n else "no data tools of its own"
        lines.append(f"- {spec.name} — {spec.role} ({tool_bit}).")
    lines.append("")
    lines.append(
        "Ask about sea conditions, fishing zones, hazards, boundaries, tides, routes or "
        f"why a catch has changed, for any position in {region.display_name_en}."
    )
    return ConversationReply(
        text="\n".join(lines),
        payload={
            "specialists": specialists_module.architecture(),
            "tool_count": total_tools,
            "region": region.region_id,
        },
    )


# --------------------------------------------------------------------------------------
# CONCEPT — answered from the curated glossary (config/glossary.yaml), matched on the
# same alias rule `is_definitional` used to route here in the first place.
# --------------------------------------------------------------------------------------


def _match_glossary_term(text: str, glossary: GlossaryConfig) -> GlossaryTerm | None:
    """Which glossary term this utterance is asking about, by alias-hit count — ties
    broken by declaration order (Python's `max` keeps the first of equal candidates)."""
    t = normalise(text).lower()
    best: GlossaryTerm | None = None
    best_hits = 0
    for term in glossary.terms:
        hits = sum(_cue_hits(t, alias.lower()) for alias in term.aliases)
        if hits > best_hits:
            best_hits = hits
            best = term
    return best


def _douglas_reference_observations(lat: float, lon: float, when: datetime) -> list[Observation]:
    """Numbers behind the `douglas_scale` glossary entry — pulled from
    `verdict/douglas.py::DOUGLAS_BANDS` itself so the prose in `config/glossary.yaml`
    cannot silently drift from the table that actually governs the ceiling."""
    from ..verdict.douglas import DOUGLAS_BANDS

    prov = Provenance(
        source_id="foreshore_glossary_douglas_reference",
        source_name="FORESHORE Douglas sea-state reference table (verdict/douglas.py)",
        authority="derived",
        url="local://reference/douglas_scale",
        acquired_at=when,
        issued_at=when,
        is_derived=True,
        notes=(
            "Static WMO Douglas sea-scale band table, not a live reading — see "
            "verdict/douglas.py::DOUGLAS_BANDS."
        ),
    )
    _, _, moderate_hi = DOUGLAS_BANDS[4]     # MODERATE upper bound
    _, _, rough_hi = DOUGLAS_BANDS[5]        # ROUGH upper bound
    return [
        Observation(
            variable="douglas_hs_boundary_m", value=moderate_hi, unit="m",
            lat=lat, lon=lon, valid_time=when, provenance=prov,
            qualifiers={"band": 4, "descriptor": "MODERATE upper bound"},
        ),
        Observation(
            variable="douglas_hs_boundary_m", value=rough_hi, unit="m",
            lat=lat, lon=lon, valid_time=when, provenance=prov,
            qualifiers={"band": 5, "descriptor": "ROUGH upper bound"},
        ),
    ]


def _kallakkadal_observations(lat: float, lon: float, when: datetime) -> list[Observation]:
    """The number behind the `kallakkadal` glossary entry — the default vessel class's
    `long_period_swell_s` threshold, from `config/vessels.yaml` via
    `config.load_vessels()`, the same value `verdict/ceiling.py`'s kallakkadal rule
    reads."""
    vessel = load_vessels().get(None)
    threshold = vessel.limit("long_period_swell_s", 15.0) or 15.0
    prov = Provenance(
        source_id="foreshore_glossary_vessel_limits",
        source_name=f"FORESHORE vessel limits ({vessel.label_en}), config/vessels.yaml",
        authority="derived",
        url="local://reference/vessel_limits",
        acquired_at=when,
        issued_at=when,
        is_derived=True,
        notes="Static configured threshold, not a live reading — see config/vessels.yaml.",
    )
    return [
        Observation(
            variable="long_period_swell_threshold_s", value=threshold, unit="s",
            lat=lat, lon=lon, valid_time=when, provenance=prov,
            qualifiers={"vessel_class": vessel.class_id},
        ),
    ]


def _region_config_addendum(key: str, region: RegionConfig) -> str:
    """Text appended to a `region_config`-sourced glossary entry from the active
    `RegionConfig` — never a hardcoded name in this module (invariant 6)."""
    if key == "marine_protected_area":
        mpas = region.geofences.get("mpa") or []
        names = ", ".join(m.get("name_en", "") for m in mpas if m.get("name_en"))
        if names:
            return f" In {region.display_name_en}, the configured park is {names}."
    return ""


def concept_reply(
    text: str,
    *,
    language: str = "en",
    glossary: GlossaryConfig | None = None,
    region: RegionConfig | None = None,
) -> ConversationReply:
    """Answer a "what is X" question from the curated glossary. Emits an `Observation`
    for every number the matched entry's prose carries a `sourced_from` for — the
    provenance rule `config/glossary.yaml` documents at its own top."""
    region = region or load_region()
    glossary = glossary or load_glossary()
    lang = _surface_language(language, region)
    when = utcnow()

    term = _match_glossary_term(text, glossary)
    if term is None:
        # classify_utterance said CONCEPT (against some glossary_terms list), but this
        # call's own glossary does not recognise the term that tripped it — safer to say
        # what FORESHORE actually knows than to fabricate a definition for it.
        return out_of_scope_reply(lang, region=region)

    body = term.ta if (lang != "en" and term.ta) else term.en
    lat, lon = region.centre

    observations: list[Observation] = []
    addendum = ""
    if term.sourced_from == "douglas_table":
        observations = _douglas_reference_observations(lat, lon, when)
    elif term.sourced_from == "vessel_limits":
        observations = _kallakkadal_observations(lat, lon, when)
    elif term.sourced_from == "region_config":
        addendum = _region_config_addendum(term.key, region)

    return ConversationReply(
        text=body + addendum,
        observations=tuple(observations),
        payload={"glossary_key": term.key, "sourced_from": term.sourced_from},
    )


# --------------------------------------------------------------------------------------
# SMALLTALK / OUT_OF_SCOPE — a real reply when a model is reachable, a fixed one when it
# is not. Both kinds are, by definition, not a marine question — nothing here ever
# touches a verdict, a geofence or a sourced number, so routing them through the model
# carries none of invariants 1-4's risk. The model is told explicitly never to invent a
# live reading; the marine pipeline is the only path that is allowed to do that.
# --------------------------------------------------------------------------------------

_CHAT_SYSTEM_PROMPT = (
    "You are FORESHORE, a marine-safety assistant for small-boat fishermen on the "
    "Palk Bay / Gulf of Mannar coast and the shore-side console that watches them. "
    "The user's message is small talk or a general question unrelated to sea "
    "conditions — answer it naturally and briefly (1-3 sentences), like any helpful "
    "assistant: greetings, thanks, general knowledge, a joke, casual chat are all "
    "fine. Never state or imply a live sea-state, weather, verdict or any other "
    "marine reading yourself — you have no access to current data here. If it fits "
    "naturally, you may mention FORESHORE can also give a live go/no-go safety "
    "reading for this coast, but do not force that into every reply."
)


def _model_chat_reply(text: str, *, fallback: str) -> str:
    """A short, model-written conversational reply, or ``fallback`` when no model is
    reachable or the call fails. Same discipline as the rest of the pipeline
    (`CLAUDE.md`: "every query goes through the model; the deterministic path is the
    net") applied to the one door that used to be canned text no matter what."""
    try:
        client = make_client()
    except Exception:  # noqa: BLE001 — client construction must never break a reply
        return fallback
    if not client.available or isinstance(client, ScriptedClient):
        return fallback
    try:
        turn = client.turn(
            _CHAT_SYSTEM_PROMPT,
            [{"role": "user", "content": text}],
            [],
            max_tokens=200,
            temperature=0.4,
        )
        return turn.text.strip() or fallback
    except Exception:  # noqa: BLE001 — a flaky provider degrades to the canned line
        return fallback


def smalltalk_reply(
    language: str = "en", *, region: RegionConfig | None = None, text: str = ""
) -> ConversationReply:
    region = region or load_region()
    _surface_language(language, region)
    fallback = (
        "Hello — FORESHORE here. It checks sea state and weather against the governing "
        "coastal bulletin, tracks fishing zones and hazards, and watches maritime "
        "boundaries near you. Ask whenever you need a reading, or ask what it can do."
    )
    reply_text = _model_chat_reply(text, fallback=fallback) if text else fallback
    return ConversationReply(text=reply_text)


def out_of_scope_reply(
    language: str = "en", *, region: RegionConfig | None = None, text: str = ""
) -> ConversationReply:
    region = region or load_region()
    _surface_language(language, region)
    fallback = (
        "FORESHORE does not answer that — it covers marine safety and fishing "
        "conditions on this coast only: sea state, weather, fishing zones, hazards, "
        "boundaries, tides, routes and the vessel go/no-go verdict. Ask about any of "
        "those for your position, or ask what it can do."
    )
    reply_text = _model_chat_reply(text, fallback=fallback) if text else fallback
    return ConversationReply(text=reply_text)


__all__ += [
    "ConversationReply",
    "distress_reply",
    "capability_reply",
    "concept_reply",
    "smalltalk_reply",
    "out_of_scope_reply",
]
