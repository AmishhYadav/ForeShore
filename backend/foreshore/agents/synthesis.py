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

from ..config import RegionConfig, env, load_region, load_vessels
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
from .language import language_name, script_language
from .runtime import AgentRuntime, NUMBER_TOKEN_RE, RunResult, check_unsourced_numbers

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
- NEVER write the internal verdict codes GO, GO_WITH_CAUTION or DO_NOT_ADVISE. They are
  database values, not words a person says. Use the plain wording you are given below.
- Open with the plain-language verdict as a complete sentence, then the reason. Do not
  open with a bare label followed by a full stop.
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
        # A number is only spoken/written into the answer text when it is a verified,
        # published one. Demo-directory numbers exist for the on-screen contact card,
        # which marks them as such — they must not leak into prose that could be read
        # aloud and dialled. See config/handoff_contacts.yaml.
        contact = f" ({h.contact})" if (h.contact and h.contact_verified) else ""
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


# --------------------------------------------------------------------------------------
# Presentation polish
# --------------------------------------------------------------------------------------
#
# Two layers, in this order, and the order is again the safety argument.
#
# 1. `normalise_prose` is deterministic and always runs. It fixes typography only —
#    stray hard breaks, doubled spaces, ASCII dashes, a trailing bullet the model left
#    behind. It cannot change a word, so it cannot change a meaning.
#
# 2. `polish_answer` is a second, optional model pass whose only job is to make the
#    already-decided answer read well. It is allowed to reorder and rephrase. It is not
#    allowed to introduce a number, change the verdict, drop the handoff, or switch
#    language — and `polish_is_safe` checks each of those against the pre-polish text
#    rather than trusting the prompt. A candidate that fails any check is discarded and
#    the unpolished text ships. Polish is cosmetic; it never gets to be load-bearing.

#: Hard line breaks, markdown bullets/headers and code fences the model sometimes emits.
_HARD_BREAK = re.compile(r"[ \t]*\n[ \t]*")
_LEADING_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
_MD_HEADER = re.compile(r"^\s*#{1,6}\s*", re.MULTILINE)
_CODE_FENCE = re.compile(r"```+")
_MD_EMPHASIS = re.compile(r"(\*\*|__|(?<!\w)\*(?!\s)|(?<!\s)\*(?!\w))")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?।])")
_MISSING_SPACE_AFTER = re.compile(r"([.!?।])(?=[A-Z஀-௿઀-૿])")


def normalise_prose(text: str) -> str:
    """Typographic cleanup only. Deterministic, lossless, always applied.

    The boat UI renders the answer as one block of prose, so a model's markdown
    scaffolding — hard line breaks, list bullets, ``**bold**``, an ASCII ``--`` — arrives
    as visible litter. None of it carries meaning here, so all of it goes. No word is
    added, removed or reordered by this function.
    """
    if not text:
        return ""
    out = _CODE_FENCE.sub("", text)
    out = _MD_HEADER.sub("", out)
    out = _LEADING_BULLET.sub("", out)
    out = _MD_EMPHASIS.sub("", out)
    out = _HARD_BREAK.sub(" ", out)
    out = out.replace(" -- ", " — ").replace("--", "—")
    out = _MULTI_SPACE.sub(" ", out)
    out = _SPACE_BEFORE_PUNCT.sub(r"\1", out)
    out = _MISSING_SPACE_AFTER.sub(r"\1 ", out)
    return out.strip()


POLISH_SYSTEM = """You are the final editor of FORESHORE, a marine safety advisory read by
fishermen about to decide whether to put to sea, and by shore operators watching a fleet.

You are given a finished answer. Every fact in it has already been decided and audited.
Your ONLY job is to make it read well: clear, calm, plain, in the same language it is
already written in.

You may: reorder sentences, join or split them, cut repetition, replace a clumsy phrase
with a plain one, fix grammar and punctuation.

You may NOT:
- state any number that is not already in the text you were given, or change one that is
- change, soften, strengthen or qualify the verdict
- remove the name of the person or place the reader is told to contact
- add advice, caveats, reassurance, greetings, sign-offs or anything you were not given
- write in any language other than the one the text is already in
- use markdown, bullets, headings, bold, or line breaks

Write {sentence_budget}. Short sentences. This may be read aloud over an engine, to
someone who left school early. Reply with the rewritten answer and nothing else."""


def _number_tokens(text: str) -> set[str]:
    """Numeric tokens as written, comma decimals folded to dots.

    Compared as a set rather than re-audited against the evidence on purpose: the
    pre-polish text has already passed the evidence audit, so the question here is only
    "did the editor invent or alter a number", and a subset check answers that exactly.
    """
    return {m.group(1).replace(",", ".") for m in NUMBER_TOKEN_RE.finditer(text or "")}


def polish_is_safe(
    original: str,
    candidate: str,
    *,
    verdict: Verdict | None,
    language: str,
) -> str | None:
    """``None`` when the rewrite may ship, otherwise the reason it may not.

    Every check compares the candidate against the *pre-polish* text. Nothing here trusts
    the prompt to have been obeyed.
    """
    cand = (candidate or "").strip()
    if not cand:
        return "empty"

    # A rewrite that is far shorter has dropped something; far longer has added something.
    if len(cand) < 0.5 * len(original) or len(cand) > 1.7 * len(original):
        return f"length {len(cand)} vs {len(original)}"

    new_numbers = _number_tokens(cand) - _number_tokens(original)
    if new_numbers:
        return f"introduced numbers {sorted(new_numbers)}"

    if _VERDICT_CODE_RE.search(cand):
        return "leaked a verdict code"

    if verdict is not None:
        lowered = cand.lower()
        mine = _plain_verdict(verdict.level, language).lower()
        if mine and mine not in lowered:
            return "dropped the verdict wording"
        for level in VERDICT_COPY:
            if level == verdict.level:
                continue
            other = _plain_verdict(level, language).lower()  # type: ignore[arg-type]
            # A different verdict's headline appearing is how a rewrite silently changes
            # the answer — the single failure this whole guard exists to catch.
            if other and other in lowered:
                return f"introduced the wording of {level}"

        if verdict.handoff is not None:
            # First token of the authority name: the rewrite may reword the title around
            # it, but the place itself has to survive.
            anchor = verdict.handoff.authority_name.split()[0] if verdict.handoff.authority_name else ""
            if anchor and anchor.lower() not in cand.lower():
                return "dropped the named handoff"

    if script_language(cand) != script_language(original):
        return "changed script/language"

    return None


def polish_answer(
    text: str,
    *,
    verdict: Verdict | None,
    language: str,
    runtime: AgentRuntime | None,
    analytical: bool = False,
) -> tuple[str, list[TraceStep], dict[str, Any]]:
    """Rewrite ``text`` for readability, or return it untouched.

    Returns ``(text, trace_steps, note)``. ``note`` always records whether polish was
    applied and, when it was not, why — a rejected rewrite is a thing the console should
    be able to show, not a thing that disappears.
    """
    cleaned = normalise_prose(text)
    note: dict[str, Any] = {"applied": False, "reason": None}

    if not cleaned:
        note["reason"] = "nothing to polish"
        return cleaned, [], note
    # The editor is a second model call per answer. `FORESHORE_POLISH=off` drops back to
    # the deterministic typography cleanup alone — for a slow venue link, or to halve the
    # token spend during development. On by default; the demo wants the polished read.
    if (env("FORESHORE_POLISH", "on") or "on").lower() in {"off", "0", "false", "no"}:
        note["reason"] = "polish disabled (FORESHORE_POLISH=off)"
        return cleaned, [], note
    if runtime is None or not runtime.client.available or _is_scripted(runtime):
        note["reason"] = "no model available"
        return cleaned, [], note

    budget = "at most six sentences" if analytical else "at most four sentences"
    system = POLISH_SYSTEM.format(sentence_budget=budget)
    prompt = (
        f"The answer is written in {language_name(language)}. Rewrite it in "
        f"{language_name(language)}.\n\n--- ANSWER TO REWRITE ---\n{cleaned}"
    )
    try:
        result = runtime.run(
            "PolishAgent", system, prompt, tool_names=[], parent_id=None, max_tokens=700
        )
    except Exception as exc:  # noqa: BLE001 — polish is cosmetic; never sink an answer
        note["reason"] = f"polish call failed: {type(exc).__name__}"
        return cleaned, [], note

    candidate = normalise_prose(humanise_verdict_codes(result.text or "", language))
    reason = polish_is_safe(cleaned, candidate, verdict=verdict, language=language)
    if reason is not None:
        note["reason"] = f"rejected: {reason}"
        return cleaned, list(result.steps), note

    note["applied"] = True
    return candidate, list(result.steps), note


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
    analytical: bool = False,
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
                text = humanise_verdict_codes(cleaned, language)
            trace = list(trace) + list(result.steps)

    # Final editor pass. Runs on whatever produced `text` — model prose or the template —
    # because the template is the one a demo is most likely to show and it reads like a
    # form. Cosmetic by construction: `polish_answer` discards any rewrite that moves a
    # number, the verdict, the handoff or the language (see `polish_is_safe`), and the
    # deterministic typography cleanup inside it runs even when no model is available.
    pre_polish = text
    text, polish_steps, polish_note = polish_answer(
        text, verdict=verdict, language=language, runtime=runtime, analytical=analytical
    )
    trace = list(trace) + polish_steps

    # A polished answer is re-audited rather than trusted. The rewrite was already
    # constrained to the numbers it was given, so this should never fire — which is
    # exactly why it is worth asserting on the way out.
    if polish_note.get("applied"):
        residual = check_unsourced_numbers(text, observations)
        if residual:
            text = pre_polish
            polish_note = {"applied": False, "reason": f"post-audit rejected: {residual}"}

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
            # What the reader would have seen without the editor pass, and whether that
            # pass ran. Staleness, downgrades and now rewrites are all surfaced, never
            # hidden — the console renders this next to the trace.
            "unpolished_text": pre_polish,
            "polish": polish_note,
        },
        unsourced_numbers=unsourced,
    )


#: The verdict codes are storage values, not speech. A model asked to "state the verdict
#: first" will happily open with "DO_NOT_ADVISE." — which is what a fisherman actually saw
#: on screen. The prompt forbids it and this strips it if it appears anyway; belt and
#: braces, because the prose path is the one a person reads aloud on a boat.
_VERDICT_CODE_RE = re.compile(r"\b(GO_WITH_CAUTION|DO_NOT_ADVISE|GO)\b")


def _plain_verdict(level: VerdictLevel, lang: str) -> str:
    copy = VERDICT_COPY.get(level, VERDICT_COPY["DO_NOT_ADVISE"])
    return (copy.get(lang) or copy["en"])["headline"]


def humanise_verdict_codes(text: str, lang: str) -> str:
    """Replace any bare verdict code in prose with its plain-language wording.

    Also drops a leading "<code>." sentence outright rather than leaving a stranded
    headline followed by the same thing said properly.
    """
    if not text:
        return text

    def repl(m: re.Match[str]) -> str:
        return _plain_verdict(m.group(1), lang)  # type: ignore[arg-type]

    stripped = text.lstrip()
    lead = _VERDICT_CODE_RE.match(stripped)
    if lead and stripped[lead.end():lead.end() + 1] in {".", ":", "\u2014", "-"}:
        stripped = stripped[lead.end() + 1:].lstrip()
        stripped = f"{_plain_verdict(lead.group(1), lang)}. {stripped}"  # type: ignore[arg-type]
        text = stripped
    return _VERDICT_CODE_RE.sub(repl, text)


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
            "Say it in these words, never as the code above: "
            f"\"{_plain_verdict(verdict.level, language)}\"",
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
                + (f" ({h.contact})" if (h.contact and h.contact_verified) else "")
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
    "humanise_verdict_codes", "normalise_prose", "polish_answer", "polish_is_safe",
    "POLISH_SYSTEM",
]
