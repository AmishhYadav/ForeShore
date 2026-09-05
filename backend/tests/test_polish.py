"""The final editor pass is cosmetic, and these tests are what keep it cosmetic.

`polish_answer` lets a model rewrite an answer that has already been decided and audited.
That is only safe while every way a rewrite could change the meaning is caught and the
rewrite discarded — so each guard in `polish_is_safe` gets a case here, and
`normalise_prose` is pinned as a pure typography function that never touches a word.
"""

from __future__ import annotations

import pytest

from foreshore.agents.synthesis import (
    normalise_prose,
    polish_answer,
    polish_is_safe,
)
from foreshore.models import Handoff, Verdict


ORIGINAL = (
    "Do not go. The IMD bulletin expired 7.0 hours ago and names the sea state as ROUGH. "
    "Wave height is 0.56 m. Contact Rameswaram Fishing Harbour."
)


def _verdict() -> Verdict:
    v = Verdict(level="DO_NOT_ADVISE", reasons=[])
    v.handoff = Handoff(
        reason="test",
        authority_name="Rameswaram Fishing Harbour — Harbour Master",
        authority_type="landing_centre",
    )
    return v


def _check(candidate: str) -> str | None:
    return polish_is_safe(ORIGINAL, candidate, verdict=_verdict(), language="en")


# -- normalise_prose: typography only ---------------------------------------------------


def test_normalise_strips_markdown_scaffolding_without_changing_words():
    raw = "Do not go.  \n**The bulletin** has expired.\n- Contact Rameswaram Harbour."
    out = normalise_prose(raw)
    assert out == "Do not go. The bulletin has expired. Contact Rameswaram Harbour."
    assert "\n" not in out and "*" not in out


def test_normalise_is_idempotent():
    once = normalise_prose(ORIGINAL)
    assert normalise_prose(once) == once


def test_normalise_never_invents_or_drops_a_number():
    raw = "Waves 0.56 m, wind 13.88 kn.  \n\nGusting 17.28 kn."
    out = normalise_prose(raw)
    for token in ("0.56", "13.88", "17.28"):
        assert token in out


# -- polish_is_safe: one case per way a rewrite could change the answer ------------------


def test_a_faithful_rewrite_is_allowed():
    assert (
        _check(
            "Do not go. The IMD bulletin expired 7.0 hours ago and calls the sea rough. "
            "Waves are 0.56 m. Speak to Rameswaram Fishing Harbour before you decide."
        )
        is None
    )


def test_a_rewrite_that_invents_a_number_is_rejected():
    # Same length and same everything else as the original — the invented 3.4 m is the
    # only difference, so this pins the number guard specifically rather than tripping
    # the length guard on the way past.
    reason = _check(
        "Do not go. The IMD bulletin expired 7.0 hours ago and names the sea as rough, "
        "with waves of 3.4 m. Contact Rameswaram Fishing Harbour."
    )
    assert reason is not None and "introduced numbers" in reason


def test_a_rewrite_that_changes_the_verdict_is_rejected():
    reason = _check(
        "Go with caution. The bulletin expired 7.0 hours ago. "
        "Contact Rameswaram Fishing Harbour if unsure."
    )
    assert reason is not None


def test_a_rewrite_that_drops_the_named_handoff_is_rejected():
    reason = _check(
        "Do not go. The IMD bulletin expired 7.0 hours ago and names the sea as rough, "
        "and the wave height of 0.56 m does not change that at all today."
    )
    assert reason == "dropped the named handoff"


def test_a_rewrite_that_leaks_a_verdict_code_is_rejected():
    reason = _check(
        "DO_NOT_ADVISE. The bulletin expired 7.0 hours ago and waves are 0.56 m. "
        "Contact Rameswaram Fishing Harbour."
    )
    assert reason == "leaked a verdict code"


def test_a_rewrite_that_switches_script_is_rejected():
    # A wholesale translation trips the verdict-wording guard first; that is a rejection
    # either way. This asserts the rejection, then pins the script guard on its own with
    # a candidate that keeps the English verdict wording and switches the rest.
    assert (
        _check(
            "போக வேண்டாம். வானிலை அறிக்கை 7.0 மணி நேரத்திற்கு முன் காலாவதியானது, அலை 0.56 m. "
            "Rameswaram Fishing Harbour ஐ தொடர்பு கொள்ளுங்கள் இப்போது."
        )
        is not None
    )
    assert (
        polish_is_safe(
            "The bulletin expired 7.0 hours ago and the sea is rough near the harbour.",
            "வானிலை அறிக்கை 7.0 மணி நேரத்திற்கு முன் காலாவதியானது, கடல் கொந்தளிப்பாக உள்ளது.",
            verdict=None,
            language="en",
        )
        == "changed script/language"
    )


@pytest.mark.parametrize("candidate", ["", "   ", "Do not go."])
def test_an_empty_or_gutted_rewrite_is_rejected(candidate: str):
    assert _check(candidate) is not None


# -- polish_answer: degrades to deterministic cleanup -----------------------------------


def test_polish_without_a_model_still_cleans_typography_and_says_so():
    text, steps, note = polish_answer(
        "Do not go.  \n**Waves** are 0.56 m.",
        verdict=_verdict(),
        language="en",
        runtime=None,
    )
    assert text == "Do not go. Waves are 0.56 m."
    assert steps == []
    assert note == {"applied": False, "reason": "no model available"}
