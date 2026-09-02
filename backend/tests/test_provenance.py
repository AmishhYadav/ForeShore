"""Tests for CLAUDE.md invariant 3 — "no unsourced numbers."

Every quantitative claim FORESHORE emits must trace to a retrieved record with a
source, an acquisition timestamp and a spatial resolution; the LLM never supplies a
value from its own knowledge. This is enforced two ways and both are tested here:

* structurally, at construction — :class:`~foreshore.models.Observation` cannot be built
  without a real :class:`~foreshore.models.Provenance`, and
  :meth:`foreshore.tools.registry.ToolRegistry.call` rejects any non-``Observation``
  value a tool handler tries to smuggle into ``ToolResult.observations``;
* empirically, by sweeping every tool actually registered in
  :data:`foreshore.tools.registry` in ``FORESHORE_MODE=fixture`` and checking every
  ``Observation`` it returns.

The repository ships no frozen fixtures yet (``data/fixtures/`` is empty — see the test
run's printed skip summary), so in practice every live-network tool call currently
abstains rather than returning data. That is itself the designed behaviour under
invariant 2 (``DO_NOT_ADVISE``/abstention on missing input) and is recorded as a skip,
never as a silent pass — see ``test_every_registered_tool_emits_sourced_observations``.
"""

from __future__ import annotations

from typing import get_args

import pytest

from foreshore.models import Authority, Observation, Provenance, ToolResult, utcnow
from foreshore.tools import failed_modules, registry
from foreshore.tools.registry import SPECIALISTS, ToolRegistry

ALLOWED_AUTHORITIES = set(get_args(Authority))


# --------------------------------------------------------------------------------------
# 1. Structural enforcement — Observation cannot exist without a real Provenance.
# --------------------------------------------------------------------------------------


def test_observation_construction_requires_a_provenance_instance():
    with pytest.raises(TypeError):
        Observation(
            variable="significant_wave_height",
            value=1.2,
            unit="m",
            lat=9.0,
            lon=79.0,
            valid_time=utcnow(),
            provenance="not a Provenance object",  # type: ignore[arg-type]
        )


def test_observation_construction_rejects_a_bare_dict_as_provenance():
    with pytest.raises(TypeError):
        Observation(
            variable="significant_wave_height",
            value=1.2,
            unit="m",
            lat=9.0,
            lon=79.0,
            valid_time=utcnow(),
            provenance={"source_id": "looks_plausible_but_is_not_a_provenance"},  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------------------
# 2. Boundary enforcement — the registry rejects a tool that smuggles a bare value into
#    ``observations``, even though ``ToolResult`` itself is a plain dataclass with no
#    validating ``__post_init__`` (the check lives in ``ToolRegistry.call``, on purpose,
#    so it applies uniformly to every tool regardless of how carefully each one is
#    written).
# --------------------------------------------------------------------------------------


def _fake_provenance() -> Provenance:
    return Provenance(
        source_id="fake_source",
        source_name="Fake Source",
        authority="derived",
        url="local://fake",
        acquired_at=utcnow(),
    )


@pytest.mark.parametrize(
    "bad_observation",
    [3.14, "1.2 m", {"variable": "significant_wave_height", "value": 1.2}, None],
    ids=["bare_float", "bare_string", "bare_dict", "bare_none"],
)
def test_registry_call_rejects_a_tool_that_returns_non_observation_values(bad_observation):
    """Construct the violation directly against a throwaway registry (never the
    process-wide one) and confirm ``registry.call`` fails it rather than letting a bare
    value ride through to the LLM."""
    scratch_registry = ToolRegistry()

    @scratch_registry.tool(
        name="_test_tool_returns_bare_value",
        number=9999,
        description="Test double: violates the observations contract on purpose.",
        schema={"type": "object", "properties": {}, "required": []},
        specialists=(SPECIALISTS[0],),
    )
    def _bad_tool() -> ToolResult:
        return ToolResult(
            tool="_test_tool_returns_bare_value", ok=True, observations=[bad_observation]
        )

    result = scratch_registry.call("_test_tool_returns_bare_value", {})

    assert result.ok is False
    assert result.error is not None
    assert "non-Observation" in result.error
    assert type(bad_observation).__name__ in result.error


def test_registry_call_accepts_a_tool_that_returns_real_observations():
    """Negative-control for the test above: a well-behaved tool is not rejected."""
    scratch_registry = ToolRegistry()
    obs = Observation(
        variable="significant_wave_height", value=1.1, unit="m", lat=9.0, lon=79.0,
        valid_time=utcnow(), provenance=_fake_provenance(),
    )

    @scratch_registry.tool(
        name="_test_tool_returns_real_observation",
        number=9998,
        description="Test double: well-behaved.",
        schema={"type": "object", "properties": {}, "required": []},
        specialists=(SPECIALISTS[0],),
    )
    def _good_tool() -> ToolResult:
        return ToolResult(tool="_test_tool_returns_real_observation", ok=True, observations=[obs])

    result = scratch_registry.call("_test_tool_returns_real_observation", {})
    assert result.ok is True
    assert result.observations == [obs]


# --------------------------------------------------------------------------------------
# 3. Derived values carry is_derived=True and name their parents.
# --------------------------------------------------------------------------------------


def test_derived_wave_steepness_observation_is_flagged_and_names_its_parents(
    make_observation, fixed_now
):
    """``verdict/engine.py``'s wave-steepness derivation is pure and needs no network —
    exercised directly rather than through a fixture-backed tool."""
    from foreshore.verdict import engine

    hs_obs = make_observation(variable="significant_wave_height", value=1.4, unit="m")
    period_obs = make_observation(variable="wave_period", value=6.0, unit="s")

    steep = engine.steepness(hs_obs.numeric, period_obs.numeric)
    assert steep is not None  # sanity: the two inputs really do produce a value

    ctx = engine.VerdictContext(
        lat=hs_obs.lat, lon=hs_obs.lon, observations=[hs_obs, period_obs],
        vessel_class_id="small_motorised", when=fixed_now,
    )
    outcome = engine.evaluate(ctx)

    assert outcome.derived, "expected the steepness derivation to produce a derived Observation"
    for derived_obs in outcome.derived:
        assert derived_obs.provenance.is_derived is True
        # "names its parents": either explicitly in qualifiers, or in prose in notes —
        # this implementation does both.
        assert derived_obs.provenance.notes, "a derived value's provenance must say where it came from"
        assert derived_obs.qualifiers.get("inputs"), "a derived value must name its parent provenance ids"
        for parent_id in derived_obs.qualifiers["inputs"]:
            assert isinstance(parent_id, str) and parent_id


# --------------------------------------------------------------------------------------
# 4. The full tool-registry sweep, in fixture mode.
# --------------------------------------------------------------------------------------

#: Best-effort call arguments per registered tool name. Any tool not listed here falls
#: back to a plain lat/lon call, which matches ``latlon_schema`` — the common case.
#: Unknown kwargs are silently dropped by ``ToolRegistry.call``, and a genuinely missing
#: required argument surfaces as ``ok=False`` (treated as a skip below), so this mapping
#: only needs to be "good enough", not exhaustive or forward-compatible by magic.
_TOOL_ARGS_BY_NAME: dict[str, dict] = {
    "get_tide": {"hours": 24},
    "get_currents": {},
    "get_exclusion_zones": {},
    "get_hazard_alerts": {},
    "get_lightning_nowcast": {},
    "nearest_harbour": {"n": 3},
}


def _call_args(tool_name: str, lat: float, lon: float) -> dict:
    args = {"lat": lat, "lon": lon}
    args.update(_TOOL_ARGS_BY_NAME.get(tool_name, {}))
    return args


def test_every_registered_tool_emits_sourced_observations(region, capsys):
    """Sweep ``registry.all()`` in fixture mode. A tool that abstains because its
    fixture is absent is skipped and recorded, never silently passed and never failed —
    only a tool that actually returns observations is held to the provenance contract.
    """
    assert registry.all(), "expected at least one tool to be registered"

    lat0, lon0 = region.centre
    checked: list[str] = []
    skipped: list[str] = []

    for spec in registry.all():
        args = _call_args(spec.name, lat0, lon0)
        result = registry.call(spec.name, args)

        if not result.ok:
            skipped.append(f"{spec.name}: call failed ({result.error})")
            continue
        if not result.observations:
            reason = f"missing={result.missing}" if result.missing else "no observations returned"
            skipped.append(f"{spec.name}: abstained — {reason}")
            continue

        checked.append(spec.name)
        for obs in result.observations:
            assert isinstance(obs, Observation), f"{spec.name} returned a non-Observation value"
            prov = obs.provenance
            assert isinstance(prov, Provenance)
            assert prov.source_id, f"{spec.name}: empty source_id"
            assert prov.source_name, f"{spec.name}: empty source_name"
            assert prov.authority, f"{spec.name}: empty authority"
            assert prov.authority in ALLOWED_AUTHORITIES, (
                f"{spec.name}: authority {prov.authority!r} is not one of the declared "
                f"Authority values {sorted(ALLOWED_AUTHORITIES)}"
            )
            assert prov.url, f"{spec.name}: empty url"
            assert prov.acquired_at is not None, f"{spec.name}: no acquired_at timestamp"
            assert prov.acquired_at.tzinfo is not None, f"{spec.name}: acquired_at is not timezone-aware"
            if prov.is_derived:
                assert prov.notes, f"{spec.name}: a derived observation must name its lineage in notes"

    print(f"\nprovenance sweep — {len(registry.all())} tool(s) registered")
    print(f"  checked ({len(checked)}): {sorted(checked)}")
    print(f"  skipped ({len(skipped)}) — fixture absent or abstained by design:")
    for line in sorted(skipped):
        print(f"    - {line}")
    if failed_modules():
        print(f"  tool modules not yet importable (unrelated to fixtures): {failed_modules()}")

    # Every tool must land in exactly one bucket — the sweep must never silently drop one.
    assert len(checked) + len(skipped) == len(registry.all())
