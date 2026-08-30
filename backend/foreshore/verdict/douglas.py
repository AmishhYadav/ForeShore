"""Douglas sea-state mapping.

IMD publishes ``Sea Condition`` as a descriptor *string*, not a number. Without a
deterministic mapping the advisory ceiling is unenforceable, so this module is the
hinge the whole safety argument turns on.

Two rules that are easy to get wrong and are enforced here:

* Descriptors arrive compound — ``"MODERATE; BECOMING ROUGH IN GUST"``,
  ``"SMOOTH TO SLIGHT"``, ``"SLIGHT BECOMING MODERATE TO ROUGH"``. Parse **every**
  descriptor present and take the **worst** band. Never average, never take the first.
* Longer descriptors must be matched before their own substrings: ``VERY ROUGH`` before
  ``ROUGH``, ``VERY HIGH`` before ``HIGH``. Matching is on word boundaries so that
  ``"ROUGH"`` inside ``"VERY ROUGH"`` is not double-counted.

Reference: WMO Douglas sea scale (sea state / wind sea), bands in metres of significant
wave height.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# Douglas band -> (descriptor, Hs low, Hs high). High of the top band is open-ended.
DOUGLAS_BANDS: dict[int, tuple[str, float, float]] = {
    0: ("CALM (GLASSY)", 0.00, 0.00),
    1: ("CALM (RIPPLED)", 0.00, 0.10),
    2: ("SMOOTH", 0.10, 0.50),
    3: ("SLIGHT", 0.50, 1.25),
    4: ("MODERATE", 1.25, 2.50),
    5: ("ROUGH", 2.50, 4.00),
    6: ("VERY ROUGH", 4.00, 6.00),
    7: ("HIGH", 6.00, 9.00),
    8: ("VERY HIGH", 9.00, 14.00),
    9: ("PHENOMENAL", 14.00, 99.00),
}

#: Longest first — the ordering is load-bearing, not cosmetic.
DESCRIPTOR_TO_BAND: tuple[tuple[str, int], ...] = (
    ("PHENOMENAL", 9),
    ("VERY HIGH", 8),
    ("VERY ROUGH", 6),
    ("CALM (GLASSY)", 0),
    ("CALM GLASSY", 0),
    ("GLASSY", 0),
    ("CALM (RIPPLED)", 1),
    ("CALM RIPPLED", 1),
    ("RIPPLED", 1),
    ("MODERATE", 4),
    ("SMOOTH", 2),
    ("SLIGHT", 3),
    ("ROUGH", 5),
    ("HIGH", 7),
    ("CALM", 1),
)

#: Words that mean "it may get worse" — their presence is recorded, never used to soften.
ESCALATION_MARKERS = ("BECOMING", "GUST", "SQUALL", "AT TIMES", "OCCASIONALLY", "TEMPORARILY")


@dataclass(frozen=True)
class SeaStateReading:
    """Parsed IMD sea-condition descriptor."""

    raw: str
    band: int | None
    descriptor: str | None
    hs_low_m: float | None
    hs_high_m: float | None
    #: Every band found in the string, in the order encountered.
    all_bands: tuple[int, ...] = ()
    all_descriptors: tuple[str, ...] = ()
    escalating: bool = False
    parsed: bool = False

    @property
    def label(self) -> str:
        if not self.parsed:
            return "UNPARSED"
        return f"{self.descriptor} (Douglas {self.band})"

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "band": self.band,
            "descriptor": self.descriptor,
            "hs_low_m": self.hs_low_m,
            "hs_high_m": self.hs_high_m,
            "all_bands": list(self.all_bands),
            "all_descriptors": list(self.all_descriptors),
            "escalating": self.escalating,
            "parsed": self.parsed,
        }


_WS = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WS.sub(" ", text.upper().replace(" ", " ")).strip()


def find_descriptors(text: str) -> list[tuple[str, int]]:
    """Every Douglas descriptor in ``text``, longest-match-first, no overlaps.

    Returns ``[(descriptor, band), ...]`` in the order they appear in the string.
    """
    norm = _normalise(text)
    if not norm:
        return []
    claimed = [False] * len(norm)
    found: list[tuple[int, str, int]] = []
    for phrase, band in DESCRIPTOR_TO_BAND:
        pattern = re.compile(r"(?<![A-Z])" + re.escape(phrase) + r"(?![A-Z])")
        for m in pattern.finditer(norm):
            if any(claimed[m.start(): m.end()]):
                continue          # already consumed by a longer descriptor
            for i in range(m.start(), m.end()):
                claimed[i] = True
            found.append((m.start(), phrase, band))
    found.sort()
    return [(phrase, band) for _, phrase, band in found]


def parse_sea_condition(text: str | None) -> SeaStateReading:
    """Parse an IMD ``Sea Condition`` string into the worst Douglas band it names.

    A string we cannot parse yields ``parsed=False`` and ``band=None``. Callers must
    treat that as a missing input — an unparseable ceiling cannot authorise anything.
    """
    raw = (text or "").strip()
    if not raw:
        return SeaStateReading(raw=raw, band=None, descriptor=None, hs_low_m=None, hs_high_m=None)

    hits = find_descriptors(raw)
    if not hits:
        return SeaStateReading(raw=raw, band=None, descriptor=None, hs_low_m=None, hs_high_m=None)

    bands = tuple(b for _, b in hits)
    descriptors = tuple(d for d, _ in hits)
    worst = max(bands)                      # never average, never take the first
    name, lo, hi = DOUGLAS_BANDS[worst]
    norm = _normalise(raw)
    return SeaStateReading(
        raw=raw,
        band=worst,
        descriptor=name,
        hs_low_m=lo,
        hs_high_m=hi,
        all_bands=bands,
        all_descriptors=descriptors,
        escalating=any(m in norm for m in ESCALATION_MARKERS),
        parsed=True,
    )


def band_for_hs(hs_m: float | None) -> int | None:
    """Douglas band implied by a modelled significant wave height.

    Used to state, in the evidence panel, what band a model reading corresponds to — so a
    judge can see directly that INCOIS 0.59 m is SLIGHT while IMD says MODERATE.
    """
    if hs_m is None or hs_m < 0:
        return None
    for band, (_, lo, hi) in DOUGLAS_BANDS.items():
        if band == 0:
            continue
        if lo <= hs_m < hi:
            return band
    return 9 if hs_m >= 14.0 else None


def descriptor_for_hs(hs_m: float | None) -> str | None:
    band = band_for_hs(hs_m)
    return DOUGLAS_BANDS[band][0] if band is not None else None


def worst_band(bands: Iterable[int | None]) -> int | None:
    known = [b for b in bands if b is not None]
    return max(known) if known else None


def hs_band_bounds(band: int | None) -> tuple[float | None, float | None]:
    if band is None or band not in DOUGLAS_BANDS:
        return (None, None)
    _, lo, hi = DOUGLAS_BANDS[band]
    return (lo, hi)


def bands_disagree(descriptor_band: int | None, model_hs_m: float | None) -> bool:
    """True when a modelled Hs falls outside the band the descriptor names.

    This is the three-source-disagreement detector. It does not resolve the conflict —
    resolution is the ceiling's job — it only makes the disagreement explicit.
    """
    if descriptor_band is None or model_hs_m is None:
        return False
    return band_for_hs(model_hs_m) != descriptor_band


__all__ = [
    "DOUGLAS_BANDS", "DESCRIPTOR_TO_BAND", "ESCALATION_MARKERS", "SeaStateReading",
    "parse_sea_condition", "find_descriptors", "band_for_hs", "descriptor_for_hs",
    "worst_band", "hs_band_bounds", "bands_disagree",
]
