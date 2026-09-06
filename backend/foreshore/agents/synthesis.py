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
from typing import Any, Callable, Iterable, Sequence

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
        "safety_note": "Safety note for this position and time",
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
        "safety_note": "இந்த இடத்திற்கும் நேரத்திற்கும் பாதுகாப்பு குறிப்பு",
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
        "safety_note": "આ સ્થળ અને સમય માટે સલામતી નોંધ",
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

{answer_kind_rule}

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
- Do not open with a bare label followed by a full stop. Every line is a real sentence.

Presentation — this is the text that goes on the screen, so write it finished:
- Plain prose only. No markdown, no bullets, no headings, no bold, no line breaks.
- Do not name tools, variables, source ids, file paths or error classes. The reader
  cannot call a tool and does not know what a SourceError is.
- Do not repeat a fact you have already stated, in different words or the same.
- Every sentence ends in a full stop. Never end on a bare name or a fragment.
- {sentence_budget}
"""

#: What "answer the question" means for each kind. Substituted into SYNTHESIS_SYSTEM.
#:
#: The bug this exists to kill: every answer used to open with the verdict, so "which
#: vessels are closest to the IMBL" came back as "Do not go." — a refusal-shaped reply to
#: a question that was never about going anywhere. The verdict still runs, is still shown
#: and still cannot be softened; it just stops pretending to be the answer.
ANSWER_KIND_RULES: dict[str, str] = {
    "ADVISORY": (
        "THE QUESTION IS A GO/NO-GO QUESTION. The verdict IS the answer. Open with the "
        "plain-language verdict as a complete sentence, then give the reason."
    ),
    "INFORMATIONAL": (
        "THE QUESTION IS NOT A GO/NO-GO QUESTION. It asks for a fact, a position, a "
        "count, a history or an explanation. ANSWER THAT QUESTION, from the evidence "
        "below, in your own first sentences.\n"
        "The verdict is safety context for this position and time, not the answer, and "
        "you must never present it as a refusal to answer what was asked. State it in "
        "one short sentence, using the exact plain wording you are given:\n"
        "- if the verdict is the permissive one, put that sentence LAST;\n"
        "- otherwise put it FIRST, then answer the question — a reader asking about the "
        "sea while conditions are against them needs both, in that order.\n"
        "If the evidence does not contain what was asked for, say plainly that it is not "
        "available. Do not substitute the verdict for the missing answer."
    ),
}


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


#: Sentence terminators this codebase's copy actually uses — ASCII, plus the Devanagari
#: danda already handled by :func:`strip_unsourced`'s split.
_TERMINATORS = ".!?।"
_WS = re.compile(r"\s+")


def _dedupe_key(sentence: str) -> str:
    """Normalised form two sentences are considered the same by: case, whitespace and
    trailing punctuation folded away. Tool summaries reach this function from several
    tools at once and a summary repeated verbatim is the commonest way the answer ends
    up saying one thing twice."""
    return _WS.sub(" ", sentence.strip().rstrip(_TERMINATORS).strip()).lower()


def as_sentences(parts: Iterable[str]) -> list[str]:
    """Trim, terminate and deduplicate the fragments spliced into an answer.

    Tool summaries are written to stand alone, so some end in a full stop and some do
    not; joined with a bare space, an unterminated one runs straight into the next
    ("... 0.23 nm (WARN) Nearest landing centre: ..."). This gives every fragment exactly
    one terminator and drops any it has already said.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in parts:
        s = _WS.sub(" ", (raw or "").strip())
        if not s:
            continue
        key = _dedupe_key(s)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(s if s[-1] in _TERMINATORS else s + ".")
    return out


def _verdict_block(verdict: Verdict, lang: str, *, framed: bool) -> tuple[list[str], list[str]]:
    """The verdict, said in words, split into ``(lead, detail)``.

    ``lead`` is the two sentences that must reach the reader whatever else happens: the
    plain-language verdict and its one-line consequence. ``detail`` is the audit trail —
    the ceiling's reason, the downgrade disclosure, the named handoff — which is
    load-bearing but can follow the answer to an informational question without any of
    it being lost.

    ``framed`` prefixes the lead with the "safety note" label, which is what turns the
    block from *the answer* into *context attached to an answer*.
    """
    copy = VERDICT_COPY.get(verdict.level, VERDICT_COPY["DO_NOT_ADVISE"])
    words = copy.get(lang) or copy["en"]

    head = (
        f"{label('safety_note', lang)} — {words['headline']}."
        if framed
        else words["headline"] + "."
    )
    lead: list[str] = [head, words["lead"]]
    parts: list[str] = []

    if verdict.ceiling_notes:
        parts.append(verdict.ceiling_notes[0])
    elif verdict.reasons:
        parts.append(verdict.reasons[0])

    if verdict.downgraded_from:
        # The levels themselves are deliberately NOT named here. They are storage values
        # — the same thing `humanise_verdict_codes` strips out of model prose — and this
        # line used to print them raw ("GO_WITH_CAUTION -> DO_NOT_ADVISE"), which also
        # meant every downgraded answer carried another verdict's wording and so was
        # rejected outright by `polish_is_safe`. The downgrade is a structural fact; both
        # UIs render `downgraded_from` on the verdict card. Prose says only that it
        # happened, which is what the reader needs.
        parts.append(f"{label('downgraded', lang)}.")

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

    return lead, parts


def template_answer(
    verdict: Verdict,
    lang: str,
    *,
    region: RegionConfig | None = None,
    extras: Sequence[str] = (),
    answer_kind: str = "ADVISORY",
) -> str:
    """The answer FORESHORE gives with no language model in the loop at all.

    Everything load-bearing is here: the verdict, the reason the ceiling gave, the named
    handoff. The model makes this sound human; it does not make it correct.

    ``answer_kind`` decides the *order*, never the content — the verdict block below is
    byte-identical either way apart from its framing label:

    ``ADVISORY``
        The question was "should I go". The verdict leads in full, the findings follow.
    ``INFORMATIONAL`` with a ``GO`` verdict
        The question was about the world. It is answered first and the verdict trails.
    ``INFORMATIONAL`` with any other verdict
        The two-sentence safety lead goes **first** — someone asking where the fish are
        while the bulletin has expired is told that before anything else — then the
        answer to their question, then the rest of the safety detail. CLAUDE.md's
        "favour the safety path" resolved without burying the answer or turning it into
        a refusal to answer.
    """
    region = region or load_region()
    informational = answer_kind == "INFORMATIONAL"
    lead, detail = _verdict_block(verdict, lang, framed=informational)
    findings = list(extras)

    if not informational:
        parts = lead + detail + findings
    elif verdict.level == "GO":
        parts = findings + lead + detail
    else:
        parts = lead + findings + detail

    return " ".join(as_sentences(parts))


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
    out = out.strip()
    # A model that ends on a name rather than a sentence ("... Rameswaram Fishing Harbour
    # — Harbour Master") leaves the answer looking truncated, which on a safety advisory
    # reads as something having gone wrong. Adding the stop changes no word.
    if out and out[-1] not in _TERMINATORS and out[-1] not in "\"')]":
        out += "."
    return out


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


#: Splits an authority name from the role appended to it — "Rameswaram Fishing Harbour
#: — Harbour Master" -> "Rameswaram Fishing Harbour". Prose legitimately drops the role;
#: it must not legitimately drop the place.
_AUTHORITY_ROLE_SPLIT = re.compile(r"\s*[—–(,]|\s+-\s+")


def handoff_place(handoff: Any) -> str:
    """The part of the authority name that has to survive: the named place."""
    name = (getattr(handoff, "authority_name", "") or "").strip()
    return _WS.sub(" ", _AUTHORITY_ROLE_SPLIT.split(name, maxsplit=1)[0]).strip()


def handoff_present(text: str, handoff: Any) -> bool:
    """Is the named human authority actually in this text?

    Matched on the whole place phrase, not on its first token. The first token is the
    port name, and this coast's vessels are named after their port — so a sentence
    reading "Rameswaram FB-01 and Rameswaram FB-05 are the closest" satisfied a check
    for "Rameswaram", and an answer with nobody to call sailed through both of this
    module's guards. The trailing role ("— Harbour Master") is not required: prose drops
    it naturally and the place is what a person needs.
    """
    place = handoff_place(handoff)
    if not place:
        return True
    haystack = _WS.sub(" ", text or "").lower()
    if " " in place:
        return place.lower() in haystack
    # Single-word authority names are not what the landing-centre list actually contains,
    # but a bare substring test on one would bring the vessel-name collision straight
    # back. Word boundaries are the most this can do without a name to disambiguate on.
    return re.search(rf"(?<!\w){re.escape(place.lower())}(?!\w)", haystack) is not None


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
        original_lowered = original.lower()
        for level in VERDICT_COPY:
            if level == verdict.level:
                continue
            other = _plain_verdict(level, language).lower()  # type: ignore[arg-type]
            # A different verdict's headline *appearing where it was not already* is how
            # a rewrite silently changes the answer — the single failure this whole guard
            # exists to catch. Checked against the pre-polish text rather than absolutely:
            # a phrase the original legitimately contained is not something the editor
            # introduced, and rejecting it would disable polish on those answers entirely.
            if other and other in lowered and other not in original_lowered:
                return f"introduced the wording of {level}"

        # The place itself has to survive the rewrite. Only checked when the text the
        # editor was given actually had it — polish is not the layer that puts a missing
        # handoff back (`enforce_answer_contract` is), and failing here on an input that
        # never had one would disable polish instead of fixing anything.
        if verdict.handoff is not None and handoff_present(original, verdict.handoff):
            if not handoff_present(cand, verdict.handoff):
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
    #: "model" when synthesis prose survived its audits, "template" otherwise. Decides
    #: whether the editor pass is worth a call under FORESHORE_POLISH=auto.
    written_by: str = "template",
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

    # The editor is a whole extra model call per answer — measured at 12.7 s on the free
    # NIM endpoint, on top of everything else — and on a streaming surface it rewrites
    # text the viewer has just watched being typed.
    #
    # `auto`, the default, skips it, for two reasons that both come from measurement:
    #
    # * On the model path it is redundant. SYNTHESIS_SYSTEM now carries the same
    #   presentation rules this pass used to enforce, so the prose arrives finished.
    # * On the template path it is not worth the risk. The template is deterministic and
    #   already audited, and the one thing an editor can still do to it is introduce a
    #   number — which is exactly what it did on the run that decided this default, and
    #   what `polish_is_safe` then had to catch. Spending 12 s and a fabrication risk on
    #   cosmetics is the "polish becomes load-bearing" trap, from the other end.
    #
    # `on` forces it, `off` is the same as `auto` but says so explicitly. The
    # deterministic typography cleanup above runs either way and is what actually keeps
    # the text tidy.
    setting = (env("FORESHORE_POLISH", "auto") or "auto").strip().lower()
    if setting not in {"on", "1", "true", "yes"}:
        note["reason"] = (
            "polish disabled (FORESHORE_POLISH=off)"
            if setting in {"off", "0", "false", "no"}
            else "not needed: the answer is already written to the presentation rules"
        )
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


def answers_the_question(text: str, findings: Sequence[str]) -> bool:
    """Did a model asked an informational question actually answer it?

    Checked on numbers, because on this system the substance of a finding *is* its
    numbers — a distance to a boundary, a count of vessels, a decadal trend. A model that
    shares none of the findings' numeric tokens has written about something else, and on
    an informational question that something else is invariably the verdict: a small
    model handed a ``DO_NOT_ADVISE`` will restate the advisory and never mention the
    fleet. That answer is safe and wrong, and the template — which splices the findings
    in verbatim — is the better one, so the caller falls back to it.

    Findings with no numbers at all cannot be checked this way and are not held against
    the model.
    """
    wanted = set()
    for f in findings:
        wanted |= _number_tokens(f)
    if not wanted:
        return True
    return bool(wanted & _number_tokens(text))


def enforce_answer_contract(
    text: str,
    *,
    verdict: Verdict | None,
    language: str,
    answer_kind: str,
) -> tuple[str, list[str]]:
    """Repair a model-written answer that dropped something it may not drop.

    ``polish_answer`` guards the *editor* pass against losing the verdict wording or the
    named handoff, by comparing its candidate to the text it was given. Nothing guarded
    the **synthesis** pass the same way — so a model that answered the question and
    forgot the handoff produced a ``DO_NOT_ADVISE`` answer with no human to call, which
    is invariant 2 broken in the one place it matters most. The template path never had
    this failure; the model path did, silently.

    Repairs are additive and deterministic — a missing required sentence is appended from
    the same copy the template would have used. Nothing is rewritten and nothing is
    removed. Every repair is returned so it can be recorded on the answer rather than
    hidden.
    """
    repairs: list[str] = []
    if verdict is None or not text.strip():
        return text, repairs

    out = text.strip()
    plain = _plain_verdict(verdict.level, language)

    # 1. The verdict has to be said, in words. A model that answered the question and
    #    never mentioned the advisory at all gets it appended.
    if plain.lower() not in out.lower():
        copy = VERDICT_COPY.get(verdict.level, VERDICT_COPY["DO_NOT_ADVISE"])
        words = copy.get(language) or copy["en"]
        # Framed on an informational answer, exactly as `template_answer` frames it.
        # Appending a bare "Do not go." to an answer about vessel positions reads as a
        # non-sequitur; the label is what makes it legible as attached safety context.
        head = (
            f"{label('safety_note', language)} — {plain}."
            if answer_kind == "INFORMATIONAL"
            else f"{plain}."
        )
        # Position follows the same rule `template_answer` uses: anything but the
        # permissive verdict leads, because someone asking about the sea while conditions
        # are against them needs that first. A GO trails, so the answer they asked for
        # stays the first thing they read.
        block = [head, words["lead"]]
        out = " ".join(
            as_sentences(block + [out] if verdict.level != "GO" else [out] + block)
        )
        repairs.append("verdict wording restored")

    # 2. DO_NOT_ADVISE must hand off to a named human authority. Not negotiable, and not
    #    left to the prompt: invariant 2 says the abstention names a place, never guesses.
    if verdict.level == "DO_NOT_ADVISE" and verdict.handoff is not None:
        h = verdict.handoff
        if not handoff_present(out, h):
            contact = f" ({h.contact})" if (h.contact and h.contact_verified) else ""
            dist = f", {h.distance_nm:.1f} nm" if h.distance_nm is not None else ""
            out = " ".join(
                as_sentences(
                    [out, f"{label('handoff', language)}: {h.authority_name}{contact}{dist}."]
                )
            )
            repairs.append("named handoff restored")

    # 3. On an informational answer the verdict is context, not the answer. A model that
    #    opened with the bare headline ("Do not go. The vessels closest to ...") gets the
    #    same framing label the template uses. Deterministic and purely a prefix — no word
    #    of the model's own answer is touched.
    if answer_kind == "INFORMATIONAL":
        note = label("safety_note", language)
        # Prefix match on the wording, not an exact "Do not go." — the model writes
        # "Do not go out." and "Do not go today." just as readily, and all three open an
        # answer to a question about vessels with what looks like a refusal to answer it.
        if out.lower().startswith(plain.lower()) and not out.lower().startswith(
            note.lower()
        ):
            out = f"{note} — {out}"
            repairs.append("verdict reframed as context")

    return out, repairs


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
    answer_kind: str = "ADVISORY",
    on_token: Callable[[str], None] | None = None,
) -> AgentAnswer:
    """Build the final answer. Template first, model second, audit last.

    ``answer_kind`` ("ADVISORY" | "INFORMATIONAL", decided deterministically by
    ``planner.classify_answer_kind``) governs presentation on both the template and the
    model path, and nothing else. The verdict, the ceiling and the evidence audit are
    identical for both.
    """
    region = region or load_region()
    observations: list[Observation] = []
    for r in tool_results:
        observations.extend(r.observations)
    if verdict:
        for obs in verdict.evidence:
            if obs not in observations:
                observations.append(obs)

    base = (
        template_answer(
            verdict, language, region=region, extras=extras, answer_kind=answer_kind
        )
        if verdict
        else " ".join(as_sentences(extras)) or _no_verdict_text(language)
    )

    text = base
    unsourced: list[str] = []
    repairs: list[str] = []
    # Which path actually wrote the words a person is about to read. A deterministic
    # answer is a correct answer — it carries the same verdict, evidence and trace — but
    # it must never be mistaken for a model-written one, and "the response came back
    # suspiciously fast" is not a diagnosis anyone should have to make.
    written_by = "template"
    degraded: str | None = None
    if runtime is None:
        degraded = "no runtime supplied"
    elif not runtime.client.available:
        degraded = f"{runtime.client.name} is not available"
    elif _is_scripted(runtime):
        degraded = "no provider key configured — scripted client in use"

    if runtime is not None and runtime.client.available and not _is_scripted(runtime):
        system = SYNTHESIS_SYSTEM.format(
            language_name=language_name(language),
            answer_kind_rule=ANSWER_KIND_RULES.get(
                answer_kind, ANSWER_KIND_RULES["ADVISORY"]
            ),
            sentence_budget=(
                "At most six sentences." if analytical else "At most four sentences."
            ),
        )
        prompt = _synthesis_prompt(question, verdict, tool_results, language, answer_kind)
        # The one turn whose prose a person watches being written, so the only one that
        # streams. `tool_names=[]` is what makes that safe — a streamed tool-call turn
        # would mean reassembling partial JSON arguments for output nobody reads.
        result = runtime.run(
            "SynthesisAgent", system, prompt, tool_names=[], parent_id=None,
            max_tokens=1200, on_token=on_token,
        )
        if not result.text:
            degraded = result.error or f"model returned no text ({result.stopped})"
        else:
            cleaned, unsourced = strip_unsourced(result.text, observations)
            # The model's prose only replaces the template if it survived the audit with
            # something substantial left. Otherwise the template stands.
            echo = is_prompt_echo(cleaned)
            if not cleaned or len(cleaned) < 0.4 * len(result.text):
                degraded = f"model prose failed the evidence audit ({unsourced})"
            elif echo is not None:
                # It copied its own brief into the answer. Nothing downstream can repair
                # that — the words are wrong, not the facts — so the template ships.
                degraded = f"model echoed its instructions ({echo!r})"
                repairs.append("model echoed its instructions; template used")
            elif answer_kind == "INFORMATIONAL" and not answers_the_question(
                cleaned, extras
            ):
                # It wrote about the verdict instead of the question. The template
                # carries the findings verbatim, so it is the better answer here.
                degraded = "model did not answer the question"
                repairs.append("model did not answer the question; template used")
            else:
                # Written by a model, so audited like one: the evidence audit above
                # catches an invented number, this catches a dropped invariant.
                text, contract_repairs = enforce_answer_contract(
                    humanise_verdict_codes(cleaned, language),
                    verdict=verdict,
                    language=language,
                    answer_kind=answer_kind,
                )
                repairs.extend(r for r in contract_repairs if r not in repairs)
                written_by = "model"
        trace = list(trace) + list(result.steps)

    # Final editor pass. Runs on whatever produced `text` — model prose or the template —
    # because the template is the one a demo is most likely to show and it reads like a
    # form. Cosmetic by construction: `polish_answer` discards any rewrite that moves a
    # number, the verdict, the handoff or the language (see `polish_is_safe`), and the
    # deterministic typography cleanup inside it runs even when no model is available.
    pre_polish = text
    text, polish_steps, polish_note = polish_answer(
        text, verdict=verdict, language=language, runtime=runtime,
        analytical=analytical, written_by=written_by,
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
        else:
            # The editor is allowed to reorder and rephrase, and it will happily drop the
            # "safety note" framing or reword the handoff title out of recognition. Both
            # are cheap to put back and expensive to lose, so the contract is re-enforced
            # on whatever actually ships. Idempotent: a compliant rewrite is untouched.
            text, post_repairs = enforce_answer_contract(
                text, verdict=verdict, language=language, answer_kind=answer_kind
            )
            repairs.extend(r for r in post_repairs if r not in repairs)

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
            # What shape of question this was, so both UIs can render the verdict as the
            # answer or as context without re-deriving the classification client-side.
            "answer_kind": answer_kind,
            # What the reader would have seen without the editor pass, and whether that
            # pass ran. Staleness, downgrades and now rewrites are all surfaced, never
            # hidden — the console renders this next to the trace.
            "unpolished_text": pre_polish,
            "polish": polish_note,
            # Which path wrote the words, and why it was not the model when it was not.
            # Every query is meant to go through the model; a deterministic answer is
            # correct and complete but is a fallback, and both UIs say so rather than
            # letting it pass as model prose.
            "model": {
                "client": runtime.client.name if runtime is not None else None,
                "written_by": written_by,
                "degraded_reason": degraded,
            },
            # Invariants the model-written answer dropped and this layer put back. Empty
            # on the template path and on a well-behaved model. Surfaced, never hidden —
            # a repair is a thing the console should be able to show.
            "contract_repairs": repairs,
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
    answer_kind: str = "ADVISORY",
) -> str:
    # Three labelled blocks, and the labels matter. This prompt used to interleave
    # directives with the facts they were about ("...downgraded this from
    # GO_WITH_CAUTION. Say so — that the system was made more cautious is part of the
    # answer.") — and a mid-sized model copied those sentences straight into the answer,
    # instructions and all. Separating what is TRUE from what to DO, and saying plainly
    # that the brief is not content, is the fix; `_is_prompt_echo` is the net under it.
    lines = [
        "## QUESTION",
        question,
        "",
        "## WHAT IS TRUE  (facts and numbers — the only ones you may state)",
    ]
    if verdict:
        lines += [
            f"- The advisory has been decided: \"{_plain_verdict(verdict.level, language)}\".",
            f"- Why: {'; '.join(verdict.reasons) or '(none recorded)'}",
        ]
        if verdict.ceiling_notes:
            lines.append("- Governing advisory: " + " ".join(verdict.ceiling_notes))
        if verdict.downgraded_from:
            lines.append(
                "- The advisory ceiling made this more cautious than the vessel "
                "thresholds alone would have been."
            )
        if verdict.handoff:
            h = verdict.handoff
            lines.append(
                f"- The person to contact is {h.authority_name}"
                + (f", {h.contact}" if (h.contact and h.contact_verified) else "")
                + "."
            )
    for r in tool_results:
        if not r.observations and not r.summary:
            continue
        lines.append(f"- {r.summary}")
        for obs in r.observations[:25]:
            p = obs.provenance
            res = f", {p.spatial_resolution_m/1000:.0f} km" if p.spatial_resolution_m else ""
            lines.append(
                f"    {obs.variable} = {obs.display()} [{p.source_name}"
                f"{res}, {p.freshness}"
                + (", DERIVED" if p.is_derived else "")
                + "]"
            )

    lines += ["", "## WHAT TO DO"]
    if answer_kind == "INFORMATIONAL":
        lines += [
            "Answer the question, from the facts above.",
            "The advisory is context for this position and time, not the answer — give "
            "it one short sentence, and never let it read as a refusal to answer.",
        ]
    else:
        lines += [
            "The advisory is the answer. Open with it as a complete sentence, then give "
            "the reason.",
        ]
    if verdict and verdict.downgraded_from:
        lines.append("Mention that the advisory was made more cautious.")
    if verdict and verdict.handoff:
        lines.append(
            "Tell the reader to contact the named person, in a complete sentence — "
            "never as a bare name at the end."
        )
    lines += [
        f"Write in {language_name(language)}.",
        "",
        "Reply with the answer itself and nothing else. This brief is instructions, not "
        "text to include: never quote or restate any line above that tells you what to "
        "do, and never use the words \"question\", \"brief\", \"advisory ceiling notes\" "
        "or \"handoff\" as labels in your answer.",
    ]
    return "\n".join(lines)


#: Phrases that only ever appear in the brief, never in an answer to a fisherman. A model
#: that copies its instructions produces text containing one of these; the answer is then
#: discarded for the template, which cannot do this. Cheap, deterministic, no false
#: positives — nobody warning a boat off the sea writes "the person to contact is" as a
#: heading or says "reply with the answer itself".
_PROMPT_ECHO_MARKERS: tuple[str, ...] = (
    "## question", "## what is true", "## what to do",
    "reply with the answer itself", "this brief is instructions",
    "you must state", "say so — that the system", "say so - that the system",
    "the only ones you may state", "never as a bare name",
    "is part of the answer", "write in english",
)


def is_prompt_echo(text: str) -> str | None:
    """The marker this answer copied out of its own brief, or ``None``."""
    lowered = (text or "").lower()
    for marker in _PROMPT_ECHO_MARKERS:
        if marker in lowered:
            return marker
    return None


__all__ = [
    "compose", "template_answer", "evidence_panel", "strip_unsourced", "as_sentences",
    "enforce_answer_contract", "answers_the_question", "handoff_present",
    "is_prompt_echo",
    "VERDICT_COPY", "LABELS", "label", "SYNTHESIS_SYSTEM", "ANSWER_KIND_RULES",
    "EvidenceRow",
    "humanise_verdict_codes", "normalise_prose", "polish_answer", "polish_is_safe",
    "POLISH_SYSTEM",
]
