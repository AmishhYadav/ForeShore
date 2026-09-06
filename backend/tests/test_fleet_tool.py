"""Tests for tool 17 — ``foreshore.tools.fleet_tools.find_vessels_near_boundary`` — and
the fleet-provider hook in ``foreshore.push.vessels`` it depends on.

This tool exists because the shore console's analyst query box ships with the
placeholder question "Which vessels are closest to the IMBL right now?" and, before this
tool, nothing in the registry could see the fleet at all. Covered here:

1. The cold-start path answers even with no push loop running in this process.
2. The ``"IMBL"`` alias resolves to both — and only both — IMBL boundary classes.
3. Ranked rows are sorted ascending by distance.
4. Every observation is honestly flagged simulated, and the prose says so.
5. The prose never leaks a raw enum code (geofence class or alert level).
6. An unrecognised geofence class is a designed ``missing`` outcome, not an error.
7. ``set_fleet_provider``/``current_fleet``: a registered push-loop provider is used
   verbatim; a provider that raises degrades to the cold-start fleet rather than
   propagating.
"""

from __future__ import annotations

import pytest

from foreshore.models import VesselState, utcnow
from foreshore.push.vessels import current_fleet, set_fleet_provider
from foreshore.tools.fleet_tools import find_vessels_near_boundary

#: Raw enum codes / levels that must never leak into rendered prose (CLAUDE.md: geofence
#: classes are semantically distinct, but the fisherman/operator reads English words for
#: them, never the wire code).
_FORBIDDEN_IN_PROSE = (
    "IMBL_HISTORIC_WATERS",
    "IMBL_MARITIME_BOUNDARY",
    "ECO_SENSITIVE",
    "BREACH",
    "WARN",
    "CRITICAL",
)


@pytest.fixture(autouse=True)
def _reset_fleet_provider():
    """A provider registered by one test must never leak into the next test's cold-start
    assumptions — test order must not matter."""
    yield
    set_fleet_provider(None)


def _make_vessel(vessel_id: str, name: str, *, lat: float = 9.28, lon: float = 79.30) -> VesselState:
    return VesselState(
        vessel_id=vessel_id,
        name=name,
        lat=lat,
        lon=lon,
        heading_deg=90.0,
        speed_kn=5.0,
        vessel_class="small_motorised",
        updated_at=utcnow(),
        home_port="Rameswaram",
        crew=3,
        is_simulated=True,
    )


# --------------------------------------------------------------------------------------
# 1. cold_start path — no push loop registered in this process.
# --------------------------------------------------------------------------------------


def test_no_args_returns_ranked_fleet_in_cold_start():
    result = find_vessels_near_boundary()

    assert result.ok is True
    assert result.payload["fleet_source"] == "cold_start"
    assert result.payload["vessels"], "expected a non-empty ranked fleet in cold-start"


# --------------------------------------------------------------------------------------
# 2, 3, 4, 5. "IMBL" alias — both classes only, sorted, simulated, no raw enum in prose.
# --------------------------------------------------------------------------------------


def test_imbl_alias_ranks_only_the_two_imbl_classes():
    result = find_vessels_near_boundary(geofence_class="IMBL")

    assert result.ok is True
    vessels = result.payload["vessels"]
    assert vessels, "expected at least one vessel ranked against an IMBL class"
    for row in vessels:
        assert row["geofence_class"] in ("IMBL_HISTORIC_WATERS", "IMBL_MARITIME_BOUNDARY")


def test_imbl_alias_results_are_sorted_ascending_by_distance():
    result = find_vessels_near_boundary(geofence_class="IMBL")
    distances = [row["distance_nm"] for row in result.payload["vessels"]]
    assert distances == sorted(distances)


def test_imbl_alias_observations_are_flagged_simulated_and_summary_says_so():
    result = find_vessels_near_boundary(geofence_class="IMBL")

    assert result.observations, "expected at least one sourced observation"
    for obs in result.observations:
        assert obs.qualifiers["is_simulated"] is True
    assert "simulated" in result.summary


def test_imbl_alias_summary_has_no_raw_enum_code():
    result = find_vessels_near_boundary(geofence_class="IMBL")
    for forbidden in _FORBIDDEN_IN_PROSE:
        assert forbidden not in result.summary, f"raw code {forbidden!r} leaked into prose"


# --------------------------------------------------------------------------------------
# 6. Unrecognised class name — a designed "missing" outcome, not an error.
# --------------------------------------------------------------------------------------


def test_unknown_geofence_class_is_reported_as_missing_not_an_error():
    result = find_vessels_near_boundary(geofence_class="NOT_A_CLASS")

    assert result.ok is True
    assert "geofence_class" in result.missing


# --------------------------------------------------------------------------------------
# 7. set_fleet_provider / current_fleet contract.
# --------------------------------------------------------------------------------------


def test_set_fleet_provider_is_used_verbatim_by_current_fleet():
    known = [_make_vessel("test-01", "Test Boat 1"), _make_vessel("test-02", "Test Boat 2")]
    set_fleet_provider(lambda: known)

    vessels, source = current_fleet()

    assert source == "push_loop"
    assert [v.vessel_id for v in vessels] == ["test-01", "test-02"]


def test_raising_fleet_provider_falls_back_to_cold_start():
    def _boom() -> list[VesselState]:
        raise RuntimeError("simulator exploded")

    set_fleet_provider(_boom)

    vessels, source = current_fleet()

    assert source == "cold_start"
    assert vessels, "the cold-start fallback must still produce a fleet"


def test_tool_reads_the_registered_push_loop_fleet_not_a_second_simulation():
    known = [_make_vessel("test-11", "Test Boat A"), _make_vessel("test-12", "Test Boat B")]
    set_fleet_provider(lambda: known)

    result = find_vessels_near_boundary()

    assert result.payload["fleet_source"] == "push_loop"
    assert result.payload["total_tracked"] == 2
