"""Language detection unit tests.

Answers are English-pinned right now (``FORESHORE_LANGUAGE_LOCK`` defaults to ``en`` in
``agents/orchestrator.py``) because the no-model template path splices English tool
strings into the answer, and half-translated safety copy is worse than English safety
copy. That pin is a gate over working machinery, not a removal — so these tests cover
``agents.language`` directly, without the orchestrator, and are what proves the
capability is still intact on the day the pin is lifted.

No network, no model, no fixtures: ``detect`` is pure.
"""

from __future__ import annotations

import pytest

from foreshore.agents.language import (
    DEFAULT_LANGUAGE,
    detect,
    normalise,
    script_language,
)

PALK_BAY = ("en", "ta")
SIR_CREEK = ("en", "gu")


# --------------------------------------------------------------------------------------
# Script-block detection — unambiguous, and the path a real Tamil query takes
# --------------------------------------------------------------------------------------


def test_tamil_script_is_detected():
    assert script_language("நாளை காலை கடலுக்கு போகலாமா?") == "ta"


def test_tamil_query_detects_as_ta_within_its_region():
    assert detect("நாளை காலை கடலுக்கு போகலாமா?", candidates=PALK_BAY) == "ta"


def test_gujarati_query_detects_as_gu_within_its_region():
    assert detect("કાલે સવારે દરિયામાં જવાય?", candidates=SIR_CREEK) == "gu"


def test_script_detection_ignores_latin_and_punctuation():
    """A mostly-Latin string with no Indic characters has no dominant Indic script."""
    assert script_language("is it safe to go out now?") is None


# --------------------------------------------------------------------------------------
# Region candidates gate the result — invariant 6, "region config only"
# --------------------------------------------------------------------------------------


def test_language_outside_region_candidates_is_not_returned():
    """Tamil script in a region that does not declare `ta` must not yield `ta` — the
    region config is what decides which languages exist, never the detector."""
    assert detect("நாளை காலை கடலுக்கு போகலாமா?", candidates=SIR_CREEK) != "ta"


def test_falls_back_to_a_declared_language_when_english_is_not_declared():
    assert detect("नमस्ते", candidates=("ta",)) == "ta"


# --------------------------------------------------------------------------------------
# Romanised cues — Tamil typed on a Latin phone keyboard, the common real-world case
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "kadal poga mudiyuma",
        "naalai kadal pogalama",
        "vaanilai epadi irukku",
    ],
)
def test_romanised_tamil_detects_as_ta(text):
    assert detect(text, candidates=PALK_BAY) == "ta"


def test_plain_english_stays_english():
    assert detect("can I go out fishing tomorrow morning?", candidates=PALK_BAY) == "en"


def test_empty_text_is_the_default_language():
    assert detect("", candidates=PALK_BAY) == DEFAULT_LANGUAGE
    assert detect("   ", candidates=PALK_BAY) == DEFAULT_LANGUAGE


# --------------------------------------------------------------------------------------
# Normalisation — ASR output is inconsistently composed
# --------------------------------------------------------------------------------------


def test_normalise_collapses_whitespace_and_composes():
    assert normalise("  kadal   poga\n mudiyuma  ") == "kadal poga mudiyuma"


def test_normalise_is_nfc_and_preserves_detection():
    decomposed = "நாளை காலை கடலுக்கு போகலாமா?"
    assert detect(normalise(decomposed), candidates=PALK_BAY) == "ta"
