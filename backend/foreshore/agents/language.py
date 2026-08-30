"""Language detection and mirroring.

The problem statement is explicit that interaction must be multilingual. FORESHORE
**auto-detects and mirrors** — there is no language dropdown anywhere in either UI. A
fisherman speaking Tamil gets Tamil back; the shore operator typing English gets English.

Detection is by script block first (unambiguous and instant for Indic scripts) and by a
small romanised-keyword lexicon second, because Tamil is frequently typed in Latin script
on a phone keyboard.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

#: Unicode block ranges -> ISO 639-1 code. Extend by adding a range; no code changes
#: elsewhere are needed.
SCRIPT_RANGES: tuple[tuple[int, int, str], ...] = (
    (0x0B80, 0x0BFF, "ta"),   # Tamil
    (0x0A80, 0x0AFF, "gu"),   # Gujarati
    (0x0900, 0x097F, "hi"),   # Devanagari
    (0x0C00, 0x0C7F, "te"),   # Telugu
    (0x0D00, 0x0D7F, "ml"),   # Malayalam
    (0x0C80, 0x0CFF, "kn"),   # Kannada
    (0x0980, 0x09FF, "bn"),   # Bengali
    (0x0B00, 0x0B7F, "or"),   # Odia
)

#: Romanised cues. Deliberately fishing-domain: these are the words that actually appear
#: in a transcript from a boat, and ASR mangles the rest.
ROMANISED_CUES: dict[str, tuple[str, ...]] = {
    "ta": (
        "kadal", "meen", "padagu", "vaanilai", "alai", "kaatru", "naalai", "indru",
        "pogalama", "poidalama", "epadi", "enna", "engey", "seivathu", "vanakkam",
        "thuraimugam", "meenavar", "kaatu", "neer",
    ),
    "gu": ("dariya", "machhi", "hodi", "havaman", "aavti", "kale", "kem", "kya"),
    "hi": ("samudra", "machhli", "nav", "mausam", "lehar", "hawa", "kal", "aaj"),
}

DEFAULT_LANGUAGE = "en"


def script_language(text: str) -> str | None:
    """Dominant Indic script in ``text``, if any."""
    counts: dict[str, int] = {}
    for ch in text:
        cp = ord(ch)
        for lo, hi, code in SCRIPT_RANGES:
            if lo <= cp <= hi:
                counts[code] = counts.get(code, 0) + 1
                break
    if not counts:
        return None
    return max(counts, key=counts.__getitem__)


def detect(text: str, *, candidates: Iterable[str] | None = None) -> str:
    """Detect the language of an utterance and mirror it.

    ``candidates`` restricts the result to the languages the active region declares, so a
    region swap re-homes the language behaviour with no code change.
    """
    allowed = set(candidates) if candidates else None
    text = (text or "").strip()
    if not text:
        return DEFAULT_LANGUAGE

    by_script = script_language(text)
    if by_script and (allowed is None or by_script in allowed):
        return by_script

    lowered = re.sub(r"[^a-z\s]", " ", text.lower())
    words = set(lowered.split())
    best, best_hits = DEFAULT_LANGUAGE, 0
    for code, cues in ROMANISED_CUES.items():
        if allowed is not None and code not in allowed:
            continue
        hits = sum(1 for cue in cues if cue in words)
        if hits > best_hits:
            best, best_hits = code, hits
    if best_hits >= 1:
        return best

    if allowed is not None and DEFAULT_LANGUAGE not in allowed:
        return next(iter(sorted(allowed)))
    return DEFAULT_LANGUAGE


def normalise(text: str) -> str:
    """NFC-normalise and collapse whitespace. ASR output is inconsistently composed."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text or "")).strip()


def language_name(code: str) -> str:
    return {
        "ta": "Tamil", "gu": "Gujarati", "hi": "Hindi", "te": "Telugu", "ml": "Malayalam",
        "kn": "Kannada", "bn": "Bengali", "or": "Odia", "en": "English",
    }.get(code, code)


__all__ = [
    "detect", "script_language", "normalise", "language_name",
    "SCRIPT_RANGES", "ROMANISED_CUES", "DEFAULT_LANGUAGE",
]
