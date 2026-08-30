"""The advisory ceiling.

FORESHORE never issues a verdict more permissive than the governing IMD Coastal
Bulletin for the area. It may be *more* cautious, never less.

This is implemented as a **deterministic post-check on the finished verdict object**,
after the LLM has produced it. The LLM cannot see this code, cannot argue with it, and
cannot be prompted around it. If the check trips, the verdict is downgraded, and both
the downgrade and the rule that caused it are recorded and shown in the UI.

Rules, in the order they are evaluated:

1. **Missing or unusable input** -> ``DO_NOT_ADVISE``. No bulletin, an unparseable sea
   condition, or an unknown validity window all mean the ceiling cannot be evaluated,
   and a ceiling that cannot be evaluated cannot authorise anything.
2. **Expired bulletin** (past its own 12 h validity) -> ``DO_NOT_ADVISE``.
3. **Douglas band cap** — ``vessels.yaml`` decides the most permissive verdict each
   vessel class may receive at the worst band named in the bulletin.
4. **Port signal not NIL** -> cap at ``GO_WITH_CAUTION``.
5. **Storm surge / tidal warning naming the user's district** -> cap at
   ``GO_WITH_CAUTION``; and ``DO_NOT_ADVISE`` when long-period swell (>= the class's
   ``long_period_swell_s``, default 15 s) is present — long-period swell in a shallow bay
   is the kallakkadal signature and it drowns people in flat-calm weather.

Every ``DO_NOT_ADVISE`` must hand off to a **named** human authority. That is enforced
here: the caller supplies a handoff provider, and if it yields nothing we still refuse —
we just say who to call at the regional level rather than inventing a landing centre.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Sequence

from ..config import RegionConfig, VesselClass, load_region, load_vessels
from ..models import (
    Handoff,
    Observation,
    Provenance,
    Verdict,
    VerdictLevel,
    is_more_permissive,
    utcnow,
    worst_verdict,
)
from .douglas import SeaStateReading, parse_sea_condition

#: A port signal is "NIL" only when it says so and names no signal number.
_SIGNAL_NUMBER = re.compile(r"\b(SIGNAL\s*(NO\.?|NUMBER)?\s*[IVX0-9]+|[IVX]{1,4}\s*$)", re.I)
_NON_WORD = re.compile(r"[^A-Z]+")


def port_signal_is_nil(value: str | None) -> bool:
    """True when the bulletin's Port Signal field means "nothing hoisted".

    Conservative by construction: anything we do not recognise as an explicit NIL is
    treated as a hoisted signal, which caps the verdict. Being wrong in this direction
    costs a fisherman one cautious trip; being wrong the other way costs a boat.
    """
    if value is None:
        return False
    v = value.strip().upper()
    if not v:
        return False
    if _SIGNAL_NUMBER.search(v):
        return False
    return "NIL" in v or v in {"NO SIGNAL", "NONE", "NO WARNING SIGNAL"}


def _district_tokens(name: str) -> set[str]:
    return {t for t in _NON_WORD.split(name.upper()) if len(t) > 3}


def warning_names_district(warning: str | None, district: str | None) -> bool:
    """True when a storm-surge / tidal warning names this district.

    IMD spells district names inconsistently across bulletins (KANNIYAKUMARI /
    KANYAKUMARI, THOOTHUKUDI / TUTICORIN), so match on a normalised token rather than
    on exact equality, and accept a shared prefix of >= 6 characters.
    """
    if not warning or not district:
        return False
    text = warning.upper()
    d = district.upper().strip()
    if d in text:
        return True
    tokens = _district_tokens(text)
    dnorm = _NON_WORD.sub("", d)
    for tok in tokens:
        if tok == dnorm:
            return True
        common = min(len(tok), len(dnorm))
        if common >= 6 and tok[:6] == dnorm[:6]:
            return True
    return False


def districts_named(warning: str | None, districts: Sequence[str]) -> list[str]:
    return [d for d in districts if warning_names_district(warning, d)]


# --------------------------------------------------------------------------------------


@dataclass
class CeilingInput:
    """Everything the ceiling needs. Assembled by the verdict engine, never by the LLM."""

    sea_condition: str | None
    bulletin_provenance: Provenance | None
    port_signal: str | None = None
    storm_surge_warning: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    coast_block: str | None = None
    #: Longest swell period seen at the location, seconds. Feeds the kallakkadal rule.
    swell_period_s: float | None = None
    district: str | None = None
    vessel_class_id: str | None = None
    now: datetime | None = None

    @property
    def reading(self) -> SeaStateReading:
        return parse_sea_condition(self.sea_condition)


@dataclass
class CeilingResult:
    """What the ceiling permits, and why."""

    max_allowed: VerdictLevel
    reading: SeaStateReading
    notes: list[str] = field(default_factory=list)
    rules_fired: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    expired: bool = False
    port_signal_hoisted: bool = False
    surge_district_named: bool = False
    long_period_swell: bool = False
    source: Provenance | None = None

    def to_dict(self) -> dict:
        return {
            "max_allowed": self.max_allowed,
            "sea_state": self.reading.to_dict(),
            "notes": self.notes,
            "rules_fired": self.rules_fired,
            "missing": self.missing,
            "expired": self.expired,
            "port_signal_hoisted": self.port_signal_hoisted,
            "surge_district_named": self.surge_district_named,
            "long_period_swell": self.long_period_swell,
            "source": self.source.to_dict() if self.source else None,
        }


def compute_ceiling(
    ci: CeilingInput,
    *,
    region: RegionConfig | None = None,
    vessel: VesselClass | None = None,
) -> CeilingResult:
    """Evaluate the ceiling. Pure function of its inputs — no I/O, no LLM, no randomness."""
    region = region or load_region()
    vessel = vessel or load_vessels().get(ci.vessel_class_id)
    now = ci.now or utcnow()
    reading = ci.reading

    notes: list[str] = []
    rules: list[str] = []
    missing: list[str] = []
    caps: list[VerdictLevel] = []

    # -- 1. required inputs ------------------------------------------------------------
    if ci.bulletin_provenance is None:
        missing.append("imd_coastal_bulletin")
    if not ci.sea_condition:
        missing.append("sea_condition")
    elif not reading.parsed:
        missing.append("sea_condition_parse")
        notes.append(
            f"IMD sea condition {ci.sea_condition!r} could not be mapped to a Douglas band."
        )
    if ci.valid_to is None or ci.valid_from is None:
        missing.append("bulletin_validity")

    if missing:
        rules.append("missing_required_input")
        notes.append(
            "The governing IMD bulletin could not be read or dated, so no permission can be "
            "issued from it. Missing: " + ", ".join(missing) + "."
        )
        return CeilingResult(
            max_allowed="DO_NOT_ADVISE", reading=reading, notes=notes, rules_fired=rules,
            missing=missing, source=ci.bulletin_provenance,
        )

    # -- 2. expiry ---------------------------------------------------------------------
    expired = ci.valid_to is not None and now > ci.valid_to
    if expired:
        rules.append("bulletin_expired")
        age_h = (now - ci.valid_to).total_seconds() / 3600.0
        notes.append(
            f"The IMD bulletin expired {age_h:.1f} h ago (valid to "
            f"{ci.valid_to.isoformat()}). A stale ceiling cannot authorise a trip."
        )
        caps.append("DO_NOT_ADVISE")

    # -- 3. Douglas band cap -----------------------------------------------------------
    band_cap: VerdictLevel = vessel.max_verdict_for_band(reading.band)  # type: ignore[assignment]
    caps.append(band_cap)
    rules.append("douglas_band_cap")
    notes.append(
        f"IMD gives sea condition {reading.raw!r} for {ci.coast_block or 'this coast'} — "
        f"worst band named is {reading.descriptor} (Douglas {reading.band}, Hs "
        f"{reading.hs_low_m}-{reading.hs_high_m} m). For a {vessel.label_en} that caps the "
        f"advisory at {band_cap}."
    )
    if reading.escalating and len(reading.all_bands) > 1:
        notes.append(
            "The bulletin names more than one sea state "
            f"({', '.join(reading.all_descriptors)}); the worst is taken, never the average."
        )

    # -- 4. port signal ----------------------------------------------------------------
    hoisted = ci.port_signal is not None and not port_signal_is_nil(ci.port_signal)
    if hoisted:
        rules.append("port_signal_hoisted")
        caps.append("GO_WITH_CAUTION")
        notes.append(f"A port signal is hoisted: {ci.port_signal!r}. Capped at GO_WITH_CAUTION.")

    # -- 5. storm surge / tidal warning ------------------------------------------------
    named = warning_names_district(ci.storm_surge_warning, ci.district)
    long_swell = False
    if named:
        rules.append("storm_surge_names_district")
        caps.append("GO_WITH_CAUTION")
        notes.append(
            f"IMD storm surge / tidal warning names {ci.district}: "
            f"{(ci.storm_surge_warning or '').strip()[:220]}"
        )
        threshold = vessel.limit("long_period_swell_s", 15.0) or 15.0
        if ci.swell_period_s is not None and ci.swell_period_s >= threshold:
            long_swell = True
            rules.append("kallakkadal_long_period_swell")
            caps.append("DO_NOT_ADVISE")
            notes.append(
                f"Swell period is {ci.swell_period_s:.1f} s (>= {threshold:.0f} s) while a surge "
                "warning is in force for this district. Long-period swell in a shallow bay is "
                "the kallakkadal signature: it arrives without wind and floods the shore."
            )
    elif ci.storm_surge_warning and ci.storm_surge_warning.strip().upper() not in {"NIL", ""}:
        others = districts_named(ci.storm_surge_warning, region.districts)
        if others:
            notes.append(
                "A storm surge / tidal warning is in force for "
                f"{', '.join(others)} — not for {ci.district}. Recorded, not applied as a cap."
            )

    return CeilingResult(
        max_allowed=worst_verdict(caps),
        reading=reading,
        notes=notes,
        rules_fired=rules,
        missing=missing,
        expired=expired,
        port_signal_hoisted=hoisted,
        surge_district_named=named,
        long_period_swell=long_swell,
        source=ci.bulletin_provenance,
    )


# --------------------------------------------------------------------------------------

HandoffProvider = Callable[[], Handoff | None]


def regional_handoff(reason: str, region: RegionConfig | None = None) -> Handoff:
    """Last-resort named authority when no landing centre is available.

    Still named, still contactable — abstention hands off to a person, never to silence.
    """
    region = region or load_region()
    cg = region.coast_guard or {}
    return Handoff(
        reason=reason,
        authority_name=cg.get("name", "Indian Coast Guard — Maritime Rescue"),
        authority_type="coast_guard",
        contact=str(cg.get("contact", "1554")),
    )


def apply_ceiling(
    verdict: Verdict,
    ci: CeilingInput,
    *,
    region: RegionConfig | None = None,
    vessel: VesselClass | None = None,
    handoff_provider: HandoffProvider | None = None,
) -> Verdict:
    """Post-check the finished verdict. This runs last, always, on every answer.

    Returns the same ``Verdict`` object, mutated, so callers cannot accidentally keep a
    pre-ceiling copy and render that instead.
    """
    region = region or load_region()
    vessel = vessel or load_vessels().get(ci.vessel_class_id or verdict.vessel_class)
    result = compute_ceiling(ci, region=region, vessel=vessel)

    verdict.ceiling_source = result.source
    verdict.ceiling_notes = list(result.notes)
    verdict.valid_from = verdict.valid_from or ci.valid_from
    verdict.valid_to = verdict.valid_to or ci.valid_to
    verdict.vessel_class = verdict.vessel_class or vessel.class_id

    if is_more_permissive(verdict.level, result.max_allowed):
        verdict.downgraded_from = verdict.level
        verdict.level = result.max_allowed
        verdict.ceiling_applied = True
        verdict.reasons.append(
            f"Downgraded from {verdict.downgraded_from} to {verdict.level} by the advisory "
            f"ceiling ({', '.join(result.rules_fired)})."
        )

    if verdict.level == "DO_NOT_ADVISE" and verdict.handoff is None:
        reason = (
            result.notes[0]
            if result.notes
            else "Conditions or inputs do not permit an advisory."
        )
        handoff = handoff_provider() if handoff_provider else None
        verdict.handoff = handoff or regional_handoff(reason, region)

    verdict.validate()
    return verdict


def ceiling_summary(result: CeilingResult) -> str:
    """One line for the evidence panel header."""
    if "missing_required_input" in result.rules_fired:
        return "Advisory ceiling could not be evaluated — abstaining."
    return (
        f"Governing IMD bulletin: {result.reading.descriptor} (Douglas {result.reading.band}) "
        f"-> ceiling {result.max_allowed}"
        + (f"; rules: {', '.join(result.rules_fired)}" if result.rules_fired else "")
    )


__all__ = [
    "CeilingInput", "CeilingResult", "compute_ceiling", "apply_ceiling", "ceiling_summary",
    "port_signal_is_nil", "warning_names_district", "districts_named", "regional_handoff",
    "HandoffProvider",
]
