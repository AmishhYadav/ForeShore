"""Synthesis.

Composes the answer in the detected language, attaches the evidence panel, and runs the
advisory ceiling **last**. Three things happen here in a fixed order and the order is the
safety argument:

1. The deterministic verdict is already computed (``verdict/engine.py``). Synthesis does
   not recompute it and cannot overrule it.
2. Prose is generated — by the model when a key is present, from templates otherwise.
3. The prose is **audited** against the evidence list. A number the system cannot source
   is stripped, and the strip is recorded on the answer rather than hidden.

The templates are not a degraded mode bolted on at the end. They are the primary path:
they carry the safety copy, they are bilingual, and the model's job is to sound like a
person saying the same thing — not to decide what is said.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..config import RegionConfig, load_region, load_vessels
from ..models import (
    AgentAnswer,
    Observation,
    ToolResult,
    TraceStep,
    Verdict,
    VerdictLevel,
    utcnow,
)
from ..verdict.douglas import DOUGLAS_BANDS
from .language import language_name
from .runtime import AgentRuntime, RunResult, check_unsourced_numbers

# --------------------------------------------------------------------------------------
# Verdict copy — the words a fisherman actually hears
# --------------------------------------------------------------------------------------

VERDICT_COPY: dict[VerdictLevel, dict[str, dict[str, str]]] = {
    "GO": {
        "en": {
            "headline": "Safe to go",
            "lead": "Conditions are within limits for your boat.",
        },
        "ta": {
            "headline": "போகலாம்",
            "lead": "உங்கள் படகுக்கு நிலைமைகள் வரம்புக்குள் உள்ளன.",
        },
        "gu": {
            "headline": "જઈ શકાય",
            "lead": "તમારી હોડી માટે પરિસ્થિતિ મર્યાદામાં છે.",
        },
    },
    "GO_WITH_CAUTION": {
        "en": {
            "headline": "Go with caution",
            "lead": "You may go, but conditions are near your boat's limits. Stay close in "
                    "and keep watch.",
        },
        "ta": {
            "headline": "எச்சரிக்கையுடன் போங்கள்",
            "lead": "போகலாம், ஆனால் நிலைமைகள் உங்கள் படகின் வரம்புக்கு அருகில் உள்ளன. "
                    "கரைக்கு அருகில் இருங்கள், கவனமாக இருங்கள்.",
        },
        "gu": {
            "headline": "સાવધાની સાથે જાઓ",
            "lead": "જઈ શકો છો, પણ પરિસ્થિતિ તમારી હોડીની મર્યાદા નજીક છે. કિનારા નજીક રહો.",
        },
    },
    "DO_NOT_ADVISE": {
        "en": {
            "headline": "Do not go",
            "lead": "FORESHORE cannot advise going out. Speak to a person before you decide.",
        },
        "ta": {
            "headline": "போக வேண்டாம்",
            "lead": "கடலுக்கு போக FORESHORE ஆலோசனை தர முடியாது. முடிவெடுக்கும் முன் "
                    "ஒருவரிடம் பேசுங்கள்.",
        },
        "gu": {
            "headline": "ન જાઓ",
            "lead": "FORESHORE દરિયામાં જવાની સલાહ આપી શકતું નથી. નિર્ણય પહેલાં કોઈની સાથે વાત કરો.",
        },
    },
}

LABELS: dict[str, dict[str, str]] = {
    "en": {
        "evidence": "Evidence",
        "source": "source",
        "why": "Why",
        "ceiling": "Governing advisory",
        "handoff": "Who to contact",
        "downgraded": "This advisory was made more cautious",
        "no_signal": "No signal — using the last saved advisory",
        "boundaries": "Boundaries",
        "route": "Route",
        "unavailable": "not available",
    },
    "ta": {
        "evidence": "ஆதாரம்",
        "source": "மூலம்",
        "why": "ஏன்",
        "ceiling": "ஆளும் அறிவிப்பு",
        "handoff": "யாரை தொடர்பு கொள்வது",
        "downgraded": "இந்த ஆலோசனை மேலும் எச்சரிக்கையாக மாற்றப்பட்டது",
        "no_signal": "சிக்னல் இல்லை — கடைசியாக சேமித்த ஆலோசனை",
        "boundaries": "எல்லைகள்",
        "route": "பாதை",
        "unavailable": "கிடைக்கவில்லை",
    },
    "gu": {
        "evidence": "પુરાવા",
        "source": "સ્રોત",
        "why": "શા માટે",
        "ceiling": "શાસક સલાહ",
        "handoff": "કોનો સંપર્ક કરવો",
        "downgraded": "આ સલાહ વધુ સાવધ બનાવવામાં આવી",
        "no_signal": "સિગ્નલ નથી — છેલ્લી સાચવેલી સલાહ",
        "boundaries": "સીમાઓ",
        "route": "માર્ગ",
        "unavailable": "ઉપલબ્ધ નથી",
    },
}


def label(key: str, lang: str) -> str:
    return LABELS.get(lang, LABELS["en"]).get(key, LABELS["en"].get(key, key))


SYNTHESIS_SYSTEM = """You are the synthesis layer of FORESHORE, a marine safety advisory
for small fishing boats. You are speaking to a fisherman about to decide whether to put to
sea, or to a shore operator responsible for a fleet.

You are given a verdict that has ALREADY been decided by deterministic code and capped by
the governing IMD bulletin. You cannot change it, argue with it, or soften it. Your job is
to say it clearly in the reader's own language and explain the reasoning.

Hard rules:
- Write in {language_name} and only {language_name}.
- You may state ONLY numbers that appear in the evidence below, exactly as given. Do not
  convert units, do not round differently, do not compute anything.
- Where sources disagree, say both values, name which one governs, and say why. Never
  average them and never present one as if it were the only reading.
- Anything labelled DERIVED is FORESHORE's own indicative product. Never call it an
  official advisory.
- If the verdict is DO_NOT_ADVISE, name the person or place to contact. Do not soften the
  refusal and do not offer a workaround.
- Short sentences. This may be read aloud over an engine, to someone who left school
  early. No jargon that a fisherman would not use.
- Four sentences at most unless the question was analytical.
"""


# --------------------------------------------------------------------------------------


@dataclass
class EvidenceRow:
    variable: str
    display: str
    source_name: str
    authority: str
    resolution: str
    freshness: str
    acquired_at: str
    is_derived: bool
    governs: bool = False
    #: Same key as TraceStep.provenance_ids entries (Provenance.provenance_id, i.e.
    #: "<source_id>@<issued_at or acquired_at isoformat>") — the join key the trace
    #: inspector needs to expand a step's bare provenance ids into these real rows
    #: without re-deriving the "source_id@timestamp" format client-side.
    provenance_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def evidence_panel(
    observations: Sequence[Observation], *, governing_ids: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """One row per observation: what, from whom, how fine, how fresh.

    Nothing is labelled "current" that is not — freshness is computed from the record's
    own validity window, never asserted.
    """
    gov = set(governing_ids)
    rows: list[EvidenceRow] = []
    seen: set[tuple[str, str]] = set()
    for obs in observations:
        p = obs.provenance
        key = (obs.variable, p.provenance_id)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            EvidenceRow(
                variable=obs.variable,
                display=obs.display(),
                source_name=p.source_name,
                authority=p.authority,
                resolution=(
                    f"{p.spatial_resolution_m/1000:.0f} km"
                    if p.spatial_resolution_m
                    else "point/text"
                ),
                freshness=p.freshness,
                acquired_at=p.acquired_at.isoformat(),
                is_derived=p.is_derived,
                governs=p.provenance_id in gov,
                provenance_id=p.provenance_id,
            )
        )
    return [r.to_dict() for r in rows]


def template_answer(
    verdict: Verdict,
    lang: str,
    *,
    region: RegionConfig | None = None,
    extras: Sequence[str] = (),
) -> str:
    """The answer FORESHORE gives with no language model in the loop at all.

    Everything load-bearing is here: the verdict, the reason the ceiling gave, the named
    handoff. The model makes this sound human; it does not make it correct.
    """
    region = region or load_region()
    copy = VERDICT_COPY.get(verdict.level, VERDICT_COPY["DO_NOT_ADVISE"])
    words = copy.get(lang) or copy["en"]
    parts: list[str] = [words["headline"] + ".", words["lead"]]

    if verdict.ceiling_notes:
        parts.append(verdict.ceiling_notes[0])
    elif verdict.reasons:
        parts.append(verdict.reasons[0])

    if verdict.downgraded_from:
        parts.append(
            f"{label('downgraded', lang)}: {verdict.downgraded_from} -> {verdict.level}."
        )

    if verdict.level == "DO_NOT_ADVISE" and verdict.handoff:
        h = verdict.handoff
        contact = f" ({h.contact})" if h.contact else ""
        dist = (
            f", {h.distance_nm:.1f} nm" if h.distance_nm is not None else ""
        )
        parts.append(f"{label('handoff', lang)}: {h.authority_name}{contact}{dist}.")

    parts.extend(extras)
    return " ".join(p for p in parts if p)


def strip_unsourced(text: str, evidence: Sequence[Observation]) -> tuple[str, list[str]]:
    """Remove sentences containing a number the system cannot source.

    Blunt on purpose. A sentence with an invented wave height is worse than no sentence,
    and the alternative — silently shipping it — is the failure mode this whole project
    exists to avoid.
    """
    bad = check_unsourced_numbers(text, evidence)
    if not bad:
        return text, []
    kept: list[str] = []
    removed: list[str] = []
    for sentence in re.split(r"(?<=[.!?।])\s+", text):
        if check_unsourced_numbers(sentence, evidence):
            removed.append(sentence.strip())
        else:
            kept.append(sentence.strip())
    return " ".join(kept).strip(), bad


def compose(
    *,
    query_id: str,
    question: str,
    language: str,
    verdict: Verdict | None,
    tool_results: Sequence[ToolResult],
    trace: Sequence[TraceStep],
    runtime: AgentRuntime | None = None,
    region: RegionConfig | None = None,
    governing_ids: Iterable[str] = (),
    route: Any = None,
    extras: Sequence[str] = (),
) -> AgentAnswer:
    """Build the final answer. Template first, model second, audit last."""
    region = region or load_region()
    observations: list[Observation] = []
    for r in tool_results:
        observations.extend(r.observations)
    if verdict:
        for obs in verdict.evidence:
            if obs not in observations:
                observations.append(obs)

    base = (
        template_answer(verdict, language, region=region, extras=extras)
        if verdict
        else " ".join(extras) or _no_verdict_text(language)
    )

    text = base
    unsourced: list[str] = []

    if runtime is not None and runtime.client.available and not _is_scripted(runtime):
        system = SYNTHESIS_SYSTEM.format(language_name=language_name(language))
        prompt = _synthesis_prompt(question, verdict, tool_results, language)
        result = runtime.run(
            "SynthesisAgent", system, prompt, tool_names=[], parent_id=None, max_tokens=1200
        )
        if result.text:
            cleaned, unsourced = strip_unsourced(result.text, observations)
            # The model's prose only replaces the template if it survived the audit with
            # something substantial left. Otherwise the template stands.
            if cleaned and len(cleaned) >= 0.4 * len(result.text):
                text = cleaned
            trace = list(trace) + list(result.steps)

    payloads = {r.tool: r.payload for r in tool_results if r.payload}
    return AgentAnswer(
        query_id=query_id,
        language=language,
        text=text,
        verdict=verdict,
        evidence=observations,
        trace=list(trace),
        route=route,
        payloads={
            **payloads,
            "evidence_panel": evidence_panel(observations, governing_ids=governing_ids),
            "labels": LABELS.get(language, LABELS["en"]),
            "verdict_copy": (
                (VERDICT_COPY.get(verdict.level, {}).get(language)
                 or VERDICT_COPY.get(verdict.level, {}).get("en"))
                if verdict else None
            ),
            "template_text": base,
        },
        unsourced_numbers=unsourced,
    )


def _is_scripted(runtime: AgentRuntime) -> bool:
    return runtime.client.name == "scripted"


def _no_verdict_text(lang: str) -> str:
    return {
        "en": "FORESHORE could not assemble enough evidence to answer this safely.",
        "ta": "இதற்கு பாதுகாப்பாக பதிலளிக்க போதுமான ஆதாரம் FORESHORE ஆல் சேகரிக்க முடியவில்லை.",
        "gu": "આનો સુરક્ષિત જવાબ આપવા પૂરતા પુરાવા FORESHORE એકત્ર કરી શક્યું નથી.",
    }.get(lang, "FORESHORE could not assemble enough evidence to answer this safely.")


def _synthesis_prompt(
    question: str,
    verdict: Verdict | None,
    tool_results: Sequence[ToolResult],
    language: str,
) -> str:
    lines = [f"The question asked was: {question}", ""]
    if verdict:
        lines += [
            f"DECIDED VERDICT (you cannot change this): {verdict.level}",
            f"Reasons: {'; '.join(verdict.reasons) or '(none recorded)'}",
        ]
        if verdict.ceiling_notes:
            lines.append("Governing advisory notes: " + " ".join(verdict.ceiling_notes))
        if verdict.downgraded_from:
            lines.append(
                f"The advisory ceiling downgraded this from {verdict.downgraded_from}. "
                "Say so — that the system was made more cautious is part of the answer."
            )
        if verdict.handoff:
            h = verdict.handoff
            lines.append(
                f"Named handoff you must state: {h.authority_name}"
                + (f" ({h.contact})" if h.contact else "")
            )
    lines += ["", "EVIDENCE — the complete set of numbers you may use:"]
    for r in tool_results:
        if not r.observations and not r.summary:
            continue
        lines.append(f"[{r.tool}] {r.summary}")
        for obs in r.observations[:25]:
            p = obs.provenance
            res = f", {p.spatial_resolution_m/1000:.0f} km" if p.spatial_resolution_m else ""
            lines.append(
                f"  - {obs.variable} = {obs.display()} [{p.source_name}"
                f"{res}, {p.freshness}"
                + (", DERIVED" if p.is_derived else "")
                + "]"
            )
    lines += [
        "",
        f"Write the answer in {language_name(language)}. State the verdict first.",
    ]
    return "\n".join(lines)


__all__ = [
    "compose", "template_answer", "evidence_panel", "strip_unsourced",
    "VERDICT_COPY", "LABELS", "label", "SYNTHESIS_SYSTEM", "EvidenceRow",
]
