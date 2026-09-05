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
from zoneinfo import ZoneInfo
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
    #: Wall clock. Answers "is the bulletin FORESHORE holds still current?" Defaults to
    #: `utcnow()`; pinned only by tests.
    now: datetime | None = None
    #: The departure time the user actually asked about, when they named one. Answers the
    #: different question "does the bulletin's window even cover that time?". Kept apart
    #: from `now` on purpose: conflating the two made every "tomorrow morning" question
    #: report that the bulletin had "expired N hours ago" when it was still current.
    target_time: datetime | None = None

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


#: Plain wording for the three verdict codes. Mirrors
#: agents/synthesis.py's VERDICT_COPY headlines (en) — kept in sync by hand because the
#: ceiling must not import the synthesis layer.
_PLAIN_LEVEL: dict[str, str] = {
    "GO": "Safe to go",
    "GO_WITH_CAUTION": "Go with caution",
    "DO_NOT_ADVISE": "Do not go",
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

    # -- 2. expiry, and coverage of the requested departure time -------------------------
    # Two different failures with two different sentences. "The bulletin we hold is out of
    # date" is not the same as "the bulletin is current but stops before you plan to
    # leave", and telling a fisherman the first when the second is true is a lie about the
    # data. Both cap at DO_NOT_ADVISE — a bulletin cannot authorise outside its own window
    # either way — but only the true one is said.
    expired = ci.valid_to is not None and now > ci.valid_to
    if expired:
        rules.append("bulletin_expired")
        age_h = (now - ci.valid_to).total_seconds() / 3600.0
        notes.append(
            f"The IMD bulletin expired {age_h:.1f} h ago — it was valid only to "
            f"{local_clock(ci.valid_to, region)}. A stale ceiling cannot authorise a trip."
        )
        caps.append("DO_NOT_ADVISE")
    elif ci.target_time is not None and _outside_window(ci.target_time, ci.valid_from, ci.valid_to):
        rules.append("bulletin_does_not_cover_departure")
        notes.append(
            f"The governing IMD bulletin runs only to {local_clock(ci.valid_to, region)}, "
            f"and you asked about {local_clock(ci.target_time, region)}. It cannot authorise "
            "a trip outside its own window. IMD issues a new coastal bulletin twice a day — "
            "ask again once the one covering that time is out."
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
        f"advice at \"{_PLAIN_LEVEL.get(band_cap, band_cap)}\"."
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
        notes.append(
            f"A port signal is hoisted: {ci.port_signal!r}. That caps the advice at "
            f"\"{_PLAIN_LEVEL['GO_WITH_CAUTION']}\"."
        )

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

#: Rule codes, said out loud. An unmapped code degrades to its own words rather than
#: being dropped — a reason the reader cannot see is worse than an ugly one.
_PLAIN_RULE: dict[str, str] = {
    "missing_required_input": "a required input was missing",
    "bulletin_expired": "the governing IMD bulletin is past its 12-hour validity",
    "bulletin_does_not_cover_departure": (
        "the governing IMD bulletin's window ends before the time you asked about"
    ),
    "douglas_band_cap": "the sea state IMD names is above this boat's limit",
    "port_signal_hoisted": "a port signal is hoisted",
    "storm_surge_names_district": "an IMD storm-surge warning names this district",
    "kallakkadal_long_period_swell": "long-period swell — the kallakkadal signature",
}


def local_clock(dt: datetime | None, region: RegionConfig) -> str:
    """A timestamp said the way a person says it, in the region's own timezone.

    The ceiling notes are read aloud and shown on the boat card, and an ISO-8601 UTC
    string in that position is unreadable — worse, it is in the wrong timezone for the
    person reading it. The machine-readable value stays on the provenance record and in
    the trace; this is only how it is spoken.
    """
    if dt is None:
        return "an unknown time"
    try:
        local = dt.astimezone(ZoneInfo(region.timezone))
    except Exception:  # noqa: BLE001 — a bad tz name must not sink a verdict
        local = dt
    label = local.tzname() or ""
    return local.strftime("%H:%M on %a %d %b").replace(" 0", " ") + (f" {label}" if label else "")


def _outside_window(
    target: datetime, valid_from: datetime | None, valid_to: datetime | None
) -> bool:
    """True when ``target`` falls outside the bulletin's own validity window.

    An unknown bound is not treated as a failure here — a missing validity window is
    already caught by the required-input check above, which abstains for a different and
    more specific reason.
    """
    if valid_from is not None and target < valid_from:
        return True
    if valid_to is not None and target > valid_to:
        return True
    return False


def _plain_rules(rules: Iterable[str]) -> str:
    said = [_PLAIN_RULE.get(r, r.replace("_", " ")) for r in rules]
    if not said:
        return "advisory ceiling"
    if len(said) == 1:
        return said[0]
    return "; ".join(said[:-1]) + " and " + said[-1]


HandoffProvider = Callable[[], Handoff | None]


def regional_handoff(reason: str, region: RegionConfig | None = None) -> Handoff:
    """Last-resort named authority when no landing centre is available.

    Still named, still contactable — abstention hands off to a person, never to silence.
    """
    region = region or load_region()
    cg = region.coast_guard or {}
    # 1554 is a real, published national emergency number — the one handoff contact that
    # is verified, and therefore the one the UI may render as a dialable link.
    return Handoff(
        reason=reason,
        authority_name=cg.get("name", "Indian Coast Guard — Maritime Rescue"),
        authority_type="coast_guard",
        contact=str(cg.get("contact", "1554")),
        contact_label="Maritime distress",
        contact_verified=True,
        vhf_channel="Ch 16",
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
        # This string is shown to a fisherman under "Why", so it says what happened in
        # words. The machine-readable rule codes stay on `ceiling_rules_fired` /
        # the stored trace for the console and the tests — nothing is lost, it is just
        # not the version a person has to read.
        verdict.reasons.append(
            f"The IMD bulletin is stricter than our own reading, so the advisory ceiling "
            f"lowered this from \"{_PLAIN_LEVEL.get(verdict.downgraded_from, verdict.downgraded_from)}\" "
            f"to \"{_PLAIN_LEVEL.get(verdict.level, verdict.level)}\" "
            f"({_plain_rules(result.rules_fired)})."
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
    "local_clock",
    "port_signal_is_nil", "warning_names_district", "districts_named", "regional_handoff",
    "HandoffProvider",
]
