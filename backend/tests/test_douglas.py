"""Tests for ``foreshore.verdict.douglas`` — the Douglas sea-state mapping.

CLAUDE.md is explicit that the advisory ceiling is unenforceable without this mapping,
so the two rules that are easy to get wrong get the most coverage here: compound
descriptors must take the WORST band, never an average, and an unparseable descriptor
must never be silently treated as permissive.
"""

from __future__ import annotations

import pytest

from foreshore.config import load_vessels
from foreshore.verdict.douglas import (
    DOUGLAS_BANDS,
    band_for_hs,
    bands_disagree,
    descriptor_for_hs,
    find_descriptors,
    hs_band_bounds,
    parse_sea_condition,
)

# The six descriptors CLAUDE.md's table names, with their Douglas number and Hs band
# exactly as specified there. This is the ground truth the ceiling depends on.
CLAUDE_MD_TABLE: list[tuple[str, int, float, float]] = [
    ("SMOOTH", 2, 0.10, 0.50),
    ("SLIGHT", 3, 0.50, 1.25),
    ("MODERATE", 4, 1.25, 2.50),
    ("ROUGH", 5, 2.50, 4.00),
    ("VERY ROUGH", 6, 4.00, 6.00),
    ("HIGH", 7, 6.00, 9.00),
]


# --------------------------------------------------------------------------------------
# 1. Every descriptor in the table maps exactly as CLAUDE.md specifies.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("descriptor,band,hs_low,hs_high", CLAUDE_MD_TABLE)
def test_single_descriptor_matches_claude_md_table(descriptor, band, hs_low, hs_high):
    reading = parse_sea_condition(descriptor)
    assert reading.parsed is True
    assert reading.band == band
    assert reading.descriptor == descriptor
    assert reading.hs_low_m == pytest.approx(hs_low)
    assert reading.hs_high_m == pytest.approx(hs_high)
    # The DOUGLAS_BANDS table itself must agree — parse_sea_condition is not allowed to
    # drift from the table it is supposedly built on.
    assert DOUGLAS_BANDS[band] == (descriptor, hs_low, hs_high)


@pytest.mark.parametrize("descriptor,band,hs_low,hs_high", CLAUDE_MD_TABLE)
def test_single_descriptor_is_case_insensitive(descriptor, band, hs_low, hs_high):
    """IMD bulletin text is not guaranteed upper-case; the parser upper-cases internally."""
    reading = parse_sea_condition(descriptor.lower())
    assert reading.band == band


# --------------------------------------------------------------------------------------
# 2. Compound descriptors take the WORST band, never an average.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_band,expected_descriptor",
    [
        # From CLAUDE.md directly.
        ("MODERATE; BECOMING ROUGH IN GUST", 5, "ROUGH"),
        ("SMOOTH TO SLIGHT", 3, "SLIGHT"),
        # From the module's own docstring — other real-world compound forms the parser
        # is written to handle.
        ("SLIGHT BECOMING MODERATE TO ROUGH", 5, "ROUGH"),
        # A wide spread must still take the single worst band, never (2+7)/2.
        ("SMOOTH TO HIGH", 7, "HIGH"),
        # "VERY ROUGH" must not be double-counted as a bare "ROUGH" plus something else,
        # and must itself win over a plain "ROUGH" mentioned alongside it.
        ("ROUGH BECOMING VERY ROUGH", 6, "VERY ROUGH"),
    ],
)
def test_compound_descriptor_takes_worst_band(text, expected_band, expected_descriptor):
    reading = parse_sea_condition(text)
    assert reading.parsed is True
    assert reading.band == expected_band
    assert reading.descriptor == expected_descriptor


def test_compound_descriptor_never_averages():
    """SMOOTH (2) and HIGH (7) together must give 7, never a blended ~4.5."""
    reading = parse_sea_condition("SMOOTH TO HIGH")
    assert reading.band == 7
    assert reading.band not in (4, 5)  # nowhere near an average of 2 and 7


def test_compound_descriptor_records_every_band_found_in_order():
    reading = parse_sea_condition("SLIGHT BECOMING MODERATE TO ROUGH")
    assert reading.all_bands == (3, 4, 5)
    assert reading.all_descriptors == ("SLIGHT", "MODERATE", "ROUGH")
    # worst is taken for .band, but nothing is discarded from the record
    assert reading.band == max(reading.all_bands)


def test_escalation_marker_is_recorded_not_used_to_soften():
    reading = parse_sea_condition("MODERATE; BECOMING ROUGH IN GUST")
    assert reading.escalating is True
    # escalating never pulls the band down towards the calmer descriptor
    assert reading.band == 5


@pytest.mark.parametrize(
    "text,expected",
    [
        ("VERY ROUGH", [("VERY ROUGH", 6)]),
        ("VERY HIGH", [("VERY HIGH", 8)]),
        ("CALM (GLASSY)", [("CALM (GLASSY)", 0)]),
    ],
)
def test_longer_descriptor_is_not_double_counted_as_its_substring(text, expected):
    """'ROUGH' inside 'VERY ROUGH' (and 'HIGH' inside 'VERY HIGH') must be claimed once
    by the longer phrase, not counted again as the shorter one."""
    assert find_descriptors(text) == expected


# --------------------------------------------------------------------------------------
# 3. Unparseable / empty / None descriptors do NOT silently become permissive.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [None, "", "   ", "FOGGY WITH DRIZZLE", "TBD"])
def test_unparseable_descriptor_yields_no_band(text):
    reading = parse_sea_condition(text)
    assert reading.parsed is False
    assert reading.band is None
    assert reading.descriptor is None
    assert reading.hs_low_m is None
    assert reading.hs_high_m is None


def test_unparseable_band_forces_the_most_cautious_vessel_ceiling():
    """The parser's ``band=None`` output must translate, downstream, into the most
    cautious outcome a vessel class can receive — never into a default permissive one.
    Loaded from the real config/vessels.yaml, not hardcoded here."""
    catalogue = load_vessels()
    for vessel_class in catalogue.classes.values():
        assert vessel_class.max_verdict_for_band(None) == "DO_NOT_ADVISE"


# --------------------------------------------------------------------------------------
# 4. band_for_hs / descriptor_for_hs round-trip consistently at band boundaries.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("descriptor,band,hs_low,hs_high", CLAUDE_MD_TABLE)
def test_band_for_hs_matches_descriptor_at_band_midpoint(descriptor, band, hs_low, hs_high):
    mid = (hs_low + hs_high) / 2.0
    assert band_for_hs(mid) == band
    assert descriptor_for_hs(mid) == descriptor
    # round-trip: the descriptor string parses back to the same band
    assert parse_sea_condition(descriptor_for_hs(mid)).band == band


@pytest.mark.parametrize("descriptor,band,hs_low,hs_high", CLAUDE_MD_TABLE)
def test_band_for_hs_boundary_is_lower_inclusive(descriptor, band, hs_low, hs_high):
    """``lo <= hs < hi``: the exact lower boundary belongs to this band; a value just
    below it belongs to the band underneath."""
    assert band_for_hs(hs_low) == band
    assert descriptor_for_hs(hs_low) == descriptor
    just_below = hs_low - 1e-6
    if just_below >= 0:
        assert band_for_hs(just_below) != band


@pytest.mark.parametrize("descriptor,band,hs_low,hs_high", CLAUDE_MD_TABLE)
def test_band_for_hs_just_under_upper_edge_stays_in_band(descriptor, band, hs_low, hs_high):
    just_below_high = hs_high - 1e-6
    assert band_for_hs(just_below_high) == band


def test_band_for_hs_rejects_negative_and_none():
    assert band_for_hs(None) is None
    assert band_for_hs(-0.5) is None
    assert descriptor_for_hs(None) is None


@pytest.mark.parametrize("descriptor,band,hs_low,hs_high", CLAUDE_MD_TABLE)
def test_hs_band_bounds_matches_table(descriptor, band, hs_low, hs_high):
    assert hs_band_bounds(band) == (hs_low, hs_high)


def test_hs_band_bounds_unknown_band_is_none_none():
    assert hs_band_bounds(None) == (None, None)
    assert hs_band_bounds(999) == (None, None)


# --------------------------------------------------------------------------------------
# 5. bands_disagree flags a genuine conflict and does not fire on agreement.
# --------------------------------------------------------------------------------------


def test_bands_disagree_flags_the_demo_scenario():
    """The PLAN.md headline case: IMD says MODERATE (band 4) but the INCOIS model gives
    0.594 m, which is SLIGHT (band 3) — a genuine bulletin-vs-model conflict."""
    imd_band = parse_sea_condition("MODERATE").band
    assert bands_disagree(imd_band, 0.594) is True


def test_bands_disagree_does_not_fire_on_agreement():
    imd_band = parse_sea_condition("MODERATE").band
    # 1.5 m sits inside the MODERATE band (1.25-2.50 m per the table above)
    assert bands_disagree(imd_band, 1.5) is False


@pytest.mark.parametrize("descriptor_band,model_hs_m", [(None, 1.0), (4, None), (None, None)])
def test_bands_disagree_requires_both_inputs(descriptor_band, model_hs_m):
    """No descriptor band or no model reading means there is nothing to compare —
    disagreement can never be asserted from a missing input."""
    assert bands_disagree(descriptor_band, model_hs_m) is False
