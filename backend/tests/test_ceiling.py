"""Tests for ``foreshore.verdict.ceiling`` and ``foreshore.verdict.engine`` — the safety
core of FORESHORE.

Every scenario here is built directly from :class:`~foreshore.verdict.ceiling.CeilingInput`
/ :class:`~foreshore.verdict.engine.VerdictContext`, never through a network-backed tool,
so these tests exercise the deterministic decision logic in isolation from source
adapters. Per CLAUDE.md invariant 1: FORESHORE may only ever be more cautious than the
governing IMD bulletin, never more permissive, and ``DO_NOT_ADVISE`` must always hand off
to a named human authority.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from foreshore.config import load_vessels
from foreshore.models import Handoff, Observation, Verdict, is_more_permissive
from foreshore.verdict import engine
from foreshore.verdict.ceiling import CeilingInput, apply_ceiling, compute_ceiling


def _ci(make_provenance, fixed_now, region, **overrides) -> CeilingInput:
    """A CeilingInput describing a healthy, current, unrestricted SLIGHT-sea bulletin —
    one deliberately permissive enough that any override under test is the only thing
    capping the result, unless the test overrides these defaults itself."""
    defaults = dict(
        sea_condition="SLIGHT",
        bulletin_provenance=make_provenance(source_id="imd_coastal_bulletin"),
        port_signal="NIL",
        storm_surge_warning=None,
        valid_from=fixed_now - timedelta(hours=1),
        valid_to=fixed_now + timedelta(hours=11),
        coast_block=region.source("imd_bulletin_coast_block"),
        swell_period_s=None,
        district=region.districts[0],
        vessel_class_id="small_motorised",
        now=fixed_now,
    )
    defaults.update(overrides)
    return CeilingInput(**defaults)


# --------------------------------------------------------------------------------------
# 1. Downgrade behaviour — never more permissive, may be more cautious, and left alone
#    when it already is.
# --------------------------------------------------------------------------------------


def test_ceiling_downgrades_a_verdict_more_permissive_than_the_bulletin(
    make_provenance, fixed_now, region, vessel
):
    # MODERATE (band 4) caps small_motorised at GO_WITH_CAUTION per config/vessels.yaml.
    ci = _ci(make_provenance, fixed_now, region, sea_condition="MODERATE")
    verdict = Verdict(level="GO", reasons=["deterministic thresholds said GO"], evidence=[])

    apply_ceiling(verdict, ci, region=region, vessel=vessel, handoff_provider=lambda: None)

    assert verdict.level == "GO_WITH_CAUTION"
    assert verdict.downgraded_from == "GO"
    assert verdict.ceiling_applied is True
    assert any("ceiling" in r.lower() for r in verdict.reasons)


def test_ceiling_leaves_an_already_cautious_verdict_alone(
    make_provenance, fixed_now, region, vessel
):
    # Same MODERATE bulletin (cap GO_WITH_CAUTION), but the baseline is already the most
    # cautious level — the ceiling must never relax it, and must not claim it downgraded.
    ci = _ci(make_provenance, fixed_now, region, sea_condition="MODERATE")
    verdict = Verdict(level="DO_NOT_ADVISE", reasons=["deterministic thresholds said no"], evidence=[])

    apply_ceiling(verdict, ci, region=region, vessel=vessel, handoff_provider=lambda: None)

    assert verdict.level == "DO_NOT_ADVISE"
    assert verdict.ceiling_applied is False
    assert verdict.downgraded_from is None


def test_ceiling_never_produces_a_more_permissive_level_than_it_started_with():
    """Sanity check on the ordering primitive the whole ceiling is built on."""
    assert is_more_permissive("GO", "GO_WITH_CAUTION") is True
    assert is_more_permissive("GO_WITH_CAUTION", "GO") is False
    assert is_more_permissive("DO_NOT_ADVISE", "GO") is False
    assert is_more_permissive("GO", "DO_NOT_ADVISE") is True


# --------------------------------------------------------------------------------------
# 2a. Hard override — PortSignal != NIL caps at GO_WITH_CAUTION.
# --------------------------------------------------------------------------------------


def test_port_signal_nil_does_not_cap(make_provenance, fixed_now, region):
    ci = _ci(make_provenance, fixed_now, region, port_signal="NIL")
    result = compute_ceiling(ci)
    assert result.port_signal_hoisted is False
    assert "port_signal_hoisted" not in result.rules_fired
    # SLIGHT (band 3) for small_motorised is a plain GO per config/vessels.yaml.
    assert result.max_allowed == "GO"


@pytest.mark.parametrize("hoisted_signal", ["SIGNAL III", "SIGNAL NO. 2", "Signal 1 hoisted"])
def test_hoisted_port_signal_caps_at_go_with_caution(make_provenance, fixed_now, region, hoisted_signal):
    ci = _ci(make_provenance, fixed_now, region, port_signal=hoisted_signal)
    result = compute_ceiling(ci)
    assert result.port_signal_hoisted is True
    assert "port_signal_hoisted" in result.rules_fired
    assert result.max_allowed == "GO_WITH_CAUTION"


# --------------------------------------------------------------------------------------
# 2b. Hard override — storm surge / tidal warning naming the user's district.
# --------------------------------------------------------------------------------------


def test_storm_surge_naming_district_caps_at_go_with_caution(make_provenance, fixed_now, region):
    user_district = region.districts[0]
    ci = _ci(
        make_provenance, fixed_now, region,
        district=user_district,
        storm_surge_warning=f"Storm surge / tidal warning issued for {user_district} coast.",
        swell_period_s=8.0,  # well under the 15 s kallakkadal threshold
    )
    result = compute_ceiling(ci)
    assert result.surge_district_named is True
    assert "storm_surge_names_district" in result.rules_fired
    assert result.long_period_swell is False
    assert "kallakkadal_long_period_swell" not in result.rules_fired
    assert result.max_allowed == "GO_WITH_CAUTION"


def test_storm_surge_with_long_period_swell_forces_do_not_advise(make_provenance, fixed_now, region, vessel_catalogue):
    user_district = region.districts[0]
    threshold = vessel_catalogue.get("small_motorised").limit("long_period_swell_s", 15.0)
    ci = _ci(
        make_provenance, fixed_now, region,
        district=user_district,
        storm_surge_warning=f"Storm surge / tidal warning issued for {user_district} coast.",
        swell_period_s=threshold,  # exactly at the >= threshold
    )
    result = compute_ceiling(ci)
    assert result.surge_district_named is True
    assert result.long_period_swell is True
    assert "kallakkadal_long_period_swell" in result.rules_fired
    assert result.max_allowed == "DO_NOT_ADVISE"


def test_storm_surge_naming_a_different_district_does_not_cap(make_provenance, fixed_now, region):
    user_district = region.districts[0]
    other_district = region.districts[3]
    assert user_district != other_district  # sanity: config really does have two distinct names
    ci = _ci(
        make_provenance, fixed_now, region,
        district=user_district,
        storm_surge_warning=f"Storm surge / tidal warning issued for {other_district} coast.",
        swell_period_s=20.0,  # long-period, but irrelevant since it names another district
    )
    result = compute_ceiling(ci)
    assert result.surge_district_named is False
    assert "storm_surge_names_district" not in result.rules_fired
    assert "kallakkadal_long_period_swell" not in result.rules_fired
    assert result.max_allowed == "GO"


# --------------------------------------------------------------------------------------
# 2c. Hard override — bulletin older than its 12 h validity forces DO_NOT_ADVISE.
# --------------------------------------------------------------------------------------


def test_expired_bulletin_forces_do_not_advise(make_provenance, fixed_now, region):
    ci = _ci(
        make_provenance, fixed_now, region,
        valid_from=fixed_now - timedelta(hours=20),
        valid_to=fixed_now - timedelta(hours=8),  # expired 8 h before "now"
    )
    result = compute_ceiling(ci)
    assert result.expired is True
    assert "bulletin_expired" in result.rules_fired
    assert result.max_allowed == "DO_NOT_ADVISE"


def test_current_bulletin_is_not_flagged_expired(make_provenance, fixed_now, region):
    ci = _ci(make_provenance, fixed_now, region)  # default: issued 1 h ago, valid 11 h more
    result = compute_ceiling(ci)
    assert result.expired is False
    assert "bulletin_expired" not in result.rules_fired


# --------------------------------------------------------------------------------------
# 2d. Any required input missing forces DO_NOT_ADVISE.
# --------------------------------------------------------------------------------------


def test_missing_bulletin_provenance_forces_do_not_advise(make_provenance, fixed_now, region):
    ci = _ci(make_provenance, fixed_now, region, bulletin_provenance=None)
    result = compute_ceiling(ci)
    assert result.missing == ["imd_coastal_bulletin"]
    assert "missing_required_input" in result.rules_fired
    assert result.max_allowed == "DO_NOT_ADVISE"


def test_missing_sea_condition_forces_do_not_advise(make_provenance, fixed_now, region):
    ci = _ci(make_provenance, fixed_now, region, sea_condition=None)
    result = compute_ceiling(ci)
    assert result.missing == ["sea_condition"]
    assert result.max_allowed == "DO_NOT_ADVISE"


def test_unparseable_sea_condition_forces_do_not_advise(make_provenance, fixed_now, region):
    ci = _ci(make_provenance, fixed_now, region, sea_condition="FOGGY WITH DRIZZLE")
    result = compute_ceiling(ci)
    assert result.missing == ["sea_condition_parse"]
    assert result.max_allowed == "DO_NOT_ADVISE"


@pytest.mark.parametrize("field", ["valid_from", "valid_to"])
def test_missing_validity_window_forces_do_not_advise(make_provenance, fixed_now, region, field):
    ci = _ci(make_provenance, fixed_now, region, **{field: None})
    result = compute_ceiling(ci)
    assert result.missing == ["bulletin_validity"]
    assert result.max_allowed == "DO_NOT_ADVISE"


# --------------------------------------------------------------------------------------
# 3. Every DO_NOT_ADVISE carries a Handoff naming a real authority.
# --------------------------------------------------------------------------------------


def test_do_not_advise_falls_back_to_the_named_regional_coast_guard(
    make_provenance, fixed_now, region, vessel
):
    ci = _ci(make_provenance, fixed_now, region, bulletin_provenance=None)  # forces abstention
    verdict = Verdict(level="GO", reasons=[], evidence=[])

    apply_ceiling(verdict, ci, region=region, vessel=vessel, handoff_provider=lambda: None)

    assert verdict.level == "DO_NOT_ADVISE"
    assert verdict.handoff is not None
    assert verdict.handoff.authority_type == "coast_guard"
    assert verdict.handoff.authority_name == region.coast_guard.get("name")
    assert verdict.handoff.authority_name  # never empty
    assert verdict.handoff.contact == str(region.coast_guard.get("contact"))


def test_do_not_advise_prefers_a_named_landing_centre_when_a_provider_supplies_one(
    make_provenance, fixed_now, region, vessel
):
    ci = _ci(make_provenance, fixed_now, region, bulletin_provenance=None)
    verdict = Verdict(level="GO", reasons=[], evidence=[])
    named_centre = Handoff(
        reason="Nearest named landing centre for DO_NOT_ADVISE handoff.",
        authority_name="Test Landing Centre",
        authority_type="landing_centre",
        contact=None,
        lat=region.centre[0],
        lon=region.centre[1],
        distance_nm=1.2,
    )

    apply_ceiling(verdict, ci, region=region, vessel=vessel, handoff_provider=lambda: named_centre)

    assert verdict.level == "DO_NOT_ADVISE"
    assert verdict.handoff is named_centre
    assert verdict.handoff.authority_type == "landing_centre"
    assert verdict.handoff.authority_name == "Test Landing Centre"


def test_verdict_validate_rejects_do_not_advise_with_no_handoff():
    """The dataclass-level invariant this all rests on: DO_NOT_ADVISE with no Handoff at
    all must never validate — never an invented or silently-absent handoff."""
    verdict = Verdict(level="DO_NOT_ADVISE", reasons=["abstain"], evidence=[], handoff=None)
    with pytest.raises(ValueError):
        verdict.validate()


# --------------------------------------------------------------------------------------
# 4. An LLM-proposed level may only make the verdict WORSE.
# --------------------------------------------------------------------------------------


def test_llm_proposal_cannot_override_a_more_cautious_deterministic_baseline(
    make_observation, fixed_now, region, vessel_catalogue
):
    small = vessel_catalogue.get("small_motorised")
    caution_limit = small.limit("hs_caution_m")
    assert caution_limit is not None

    # A wave height well past the caution limit drives the deterministic baseline to
    # DO_NOT_ADVISE regardless of what the (permissive) SLIGHT bulletin would allow.
    hs_obs = make_observation(
        variable="significant_wave_height",
        value=caution_limit + 2.0,
        unit="m",
        valid_time=fixed_now,
    )
    ctx = engine.VerdictContext(
        lat=region.centre[0],
        lon=region.centre[1],
        observations=[hs_obs],
        vessel_class_id="small_motorised",
        when=fixed_now,
        sea_condition="SLIGHT",  # band 3 -> ceiling cap is a plain GO; it must not bind here
        port_signal="NIL",
        storm_surge_warning=None,
        coast_block=region.source("imd_bulletin_coast_block"),
        bulletin_provenance=hs_obs.provenance,
        bulletin_valid_from=fixed_now - timedelta(hours=1),
        bulletin_valid_to=fixed_now + timedelta(hours=11),
        llm_proposed="GO",
        llm_reasons=["the model thought conditions looked fine"],
    )

    outcome = engine.evaluate(ctx)

    assert outcome.verdict.level == "DO_NOT_ADVISE"
    # This must be the engine's own guard, not the ceiling stepping in behind it — the
    # SLIGHT bulletin's cap (GO) is already more permissive than DO_NOT_ADVISE, so the
    # ceiling has nothing to do here.
    assert outcome.verdict.ceiling_applied is False
    assert any("language model proposed" in r.lower() for r in outcome.verdict.reasons)


def test_llm_proposal_is_adopted_when_it_is_more_cautious(
    make_observation, fixed_now, region, vessel_catalogue
):
    small = vessel_catalogue.get("small_motorised")
    go_limit = small.limit("hs_go_m")
    # A gentle sea that the deterministic thresholds alone would call GO...
    hs_obs = make_observation(
        variable="significant_wave_height", value=go_limit * 0.5, unit="m", valid_time=fixed_now,
    )
    ctx = engine.VerdictContext(
        lat=region.centre[0],
        lon=region.centre[1],
        observations=[hs_obs],
        vessel_class_id="small_motorised",
        when=fixed_now,
        sea_condition="SLIGHT",
        port_signal="NIL",
        storm_surge_warning=None,
        coast_block=region.source("imd_bulletin_coast_block"),
        bulletin_provenance=hs_obs.provenance,
        bulletin_valid_from=fixed_now - timedelta(hours=1),
        bulletin_valid_to=fixed_now + timedelta(hours=11),
        # ... but the LLM proposes the MORE cautious GO_WITH_CAUTION, which must win.
        llm_proposed="GO_WITH_CAUTION",
        llm_reasons=["local knowledge of a tricky sandbar here"],
    )

    outcome = engine.evaluate(ctx)

    assert outcome.verdict.level == "GO_WITH_CAUTION"
    assert "local knowledge of a tricky sandbar here" in outcome.verdict.reasons


# --------------------------------------------------------------------------------------
# 5. Vessel thresholds come from config/vessels.yaml, never from code.
# --------------------------------------------------------------------------------------


def test_different_vessel_classes_give_different_verdicts_on_identical_observations(
    make_observation, fixed_now, region, vessel_catalogue,
):
    # Sanity check the premise: the two classes really do carry different limits in the
    # config file this test loads them from.
    small = vessel_catalogue.get("small_motorised")
    catamaran = vessel_catalogue.get("fibreglass_catamaran")
    assert small.limit("hs_caution_m") != catamaran.limit("hs_caution_m")

    hs_value = 1.5  # between the catamaran's go/caution limits, and comfortably above
    hs_obs = make_observation(
        variable="significant_wave_height", value=hs_value, unit="m", valid_time=fixed_now,
    )

    def _verdict_for(vessel_class_id: str):
        ctx = engine.VerdictContext(
            lat=region.centre[0],
            lon=region.centre[1],
            observations=[hs_obs],
            vessel_class_id=vessel_class_id,
            when=fixed_now,
            # SMOOTH (band 2) caps every configured class at a plain GO, so the ceiling
            # never binds and the divergence below is purely the deterministic threshold
            # engine reading a different config entry per vessel class.
            sea_condition="SMOOTH",
            port_signal="NIL",
            storm_surge_warning=None,
            coast_block=region.source("imd_bulletin_coast_block"),
            bulletin_provenance=hs_obs.provenance,
            bulletin_valid_from=fixed_now - timedelta(hours=1),
            bulletin_valid_to=fixed_now + timedelta(hours=11),
        )
        return engine.evaluate(ctx).verdict

    small_verdict = _verdict_for("small_motorised")
    catamaran_verdict = _verdict_for("fibreglass_catamaran")

    assert small_verdict.level != catamaran_verdict.level
    assert small_verdict.level == "GO_WITH_CAUTION"
    assert catamaran_verdict.level == "DO_NOT_ADVISE"
    # Neither ceiling bound in this scenario, so the difference really is the threshold
    # engine's own use of per-class config, not a ceiling artefact.
    assert small_verdict.ceiling_applied is False
    assert catamaran_verdict.ceiling_applied is False
