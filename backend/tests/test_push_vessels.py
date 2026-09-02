"""Tests for ``foreshore.push.vessels``.

Three things are under test:

1. ``default_fleet()`` returns exactly the simulated fleet the push loop needs: 8 boats,
   every one flagged ``is_simulated=True`` (CLAUDE.md: never claim real-time AIS for
   Indian small boats), distinct ids, both of the region's first two anchor ports
   represented as ``home_port``.
2. ``advance()`` is a pure dead-reckoning step: a known heading/speed/duration moves the
   vessel the expected great-circle distance (``haversine_nm``) in the expected
   direction (``bearing_deg`` back to the start should be ~180 degrees opposite the
   travelled heading), and the input object is left untouched.
3. The scripted IMBL-bound boat's heading is not just "pointing near the line once" —
   repeatedly advancing it strictly closes the distance to ``imbl_historic_waters`` over
   several ticks, which only holds if the heading genuinely tracks the boundary.
"""

from __future__ import annotations

import copy
from itertools import pairwise

import pytest
from foreshore.models import bearing_deg, haversine_nm
from foreshore.push.vessels import _IMBL_BOAT_INDEX, advance, default_fleet
from foreshore.store.vectors import VectorStore

# --------------------------------------------------------------------------------------
# 1. default_fleet()
# --------------------------------------------------------------------------------------


def test_default_fleet_returns_eight_simulated_vessels():
    fleet = default_fleet()
    assert len(fleet) == 8
    for v in fleet:
        assert v.is_simulated is True


def test_default_fleet_has_unique_ids_and_names():
    fleet = default_fleet()
    ids = [v.vessel_id for v in fleet]
    names = [v.name for v in fleet]
    assert len(set(ids)) == len(ids)
    assert len(set(names)) == len(names)


def test_default_fleet_covers_both_home_ports(region):
    fleet = default_fleet(region)
    first_two = {p.name for p in region.anchor_ports[:2]}
    home_ports = {v.home_port for v in fleet}
    assert first_two.issubset(home_ports)


def test_default_fleet_all_small_motorised():
    fleet = default_fleet()
    assert all(v.vessel_class == "small_motorised" for v in fleet)


def test_default_fleet_is_deterministic_given_seed():
    fleet_a = default_fleet(seed=42)
    fleet_b = default_fleet(seed=42)
    for a, b in zip(fleet_a, fleet_b):
        assert a.vessel_id == b.vessel_id
        assert a.lat == b.lat
        assert a.lon == b.lon
        assert a.heading_deg == b.heading_deg
        assert a.speed_kn == b.speed_kn


def test_default_fleet_default_seed_is_stable_across_runs():
    fleet_a = default_fleet()
    fleet_b = default_fleet(seed=0)
    for a, b in zip(fleet_a, fleet_b):
        assert a.lat == b.lat and a.lon == b.lon and a.heading_deg == b.heading_deg


def test_different_seeds_can_produce_different_fleets():
    fleet_a = default_fleet(seed=1)
    fleet_b = default_fleet(seed=2)
    # At least one non-scripted boat's heading should differ between seeds — otherwise
    # the "local random.Random(seed)" contract isn't actually wired up to anything.
    headings_a = [v.heading_deg for v in fleet_a]
    headings_b = [v.heading_deg for v in fleet_b]
    assert headings_a != headings_b


# --------------------------------------------------------------------------------------
# 2. advance()
# --------------------------------------------------------------------------------------


def test_advance_moves_expected_distance_and_bearing():
    import dataclasses

    fleet = default_fleet()
    vessel = fleet[1]  # a non-scripted boat is fine for a plain dead-reckoning check
    heading = 90.0
    speed_kn = 6.0
    seconds = 3600.0  # exactly one hour -> should travel ~6 nm

    # advance() uses the vessel's own heading/speed, so build a controlled fixture
    # instead of relying on the fleet's randomised heading for this assertion.
    controlled = dataclasses.replace(vessel, heading_deg=heading, speed_kn=speed_kn)
    moved = advance(controlled, seconds)

    travelled_nm = haversine_nm(controlled.lat, controlled.lon, moved.lat, moved.lon)
    assert travelled_nm == pytest.approx(speed_kn, rel=0.01)

    actual_bearing = bearing_deg(controlled.lat, controlled.lon, moved.lat, moved.lon)
    assert actual_bearing == pytest.approx(heading, abs=0.5)


def test_advance_updates_timestamp_and_leaves_everything_else_unchanged():
    import dataclasses
    from datetime import timedelta

    fleet = default_fleet()
    vessel = dataclasses.replace(fleet[2], heading_deg=45.0, speed_kn=5.0)
    seconds = 900.0

    moved = advance(vessel, seconds)

    assert moved.updated_at == vessel.updated_at + timedelta(seconds=seconds)
    assert moved.vessel_id == vessel.vessel_id
    assert moved.name == vessel.name
    assert moved.heading_deg == vessel.heading_deg
    assert moved.speed_kn == vessel.speed_kn
    assert moved.vessel_class == vessel.vessel_class
    assert moved.home_port == vessel.home_port
    assert moved.is_simulated == vessel.is_simulated


def test_advance_does_not_mutate_its_input():
    import dataclasses

    fleet = default_fleet()
    vessel = dataclasses.replace(fleet[3], heading_deg=10.0, speed_kn=4.0)
    before = copy.deepcopy(vessel)

    advance(vessel, 1800.0)

    assert vessel.lat == before.lat
    assert vessel.lon == before.lon
    assert vessel.updated_at == before.updated_at


def test_advance_with_zero_speed_is_a_position_no_op():
    import dataclasses

    fleet = default_fleet()
    vessel = dataclasses.replace(fleet[0], speed_kn=0.0)

    moved = advance(vessel, 3600.0)

    assert moved.lat == vessel.lat
    assert moved.lon == vessel.lon
    assert moved.lat == moved.lat  # not NaN
    assert moved.lon == moved.lon  # not NaN


# --------------------------------------------------------------------------------------
# 3. Scripted IMBL-bound boat actually closes the line over repeated ticks.
# --------------------------------------------------------------------------------------


def test_imbl_bound_boat_strictly_closes_distance_over_several_ticks(region):
    fleet = default_fleet(region)
    vessel = fleet[_IMBL_BOAT_INDEX]
    store = VectorStore()

    hits = store.nearest("imbl_historic_waters", vessel.lat, vessel.lon, n=1)
    if not hits:
        pytest.skip("imbl_historic_waters static layer not present in this checkout")

    distances = [hits[0].distance_nm]
    for _ in range(6):
        vessel = advance(vessel, 600.0)  # 10-minute ticks
        hits = store.nearest("imbl_historic_waters", vessel.lat, vessel.lon, n=1)
        distances.append(hits[0].distance_nm)

    for earlier, later in pairwise(distances):
        assert later < earlier, f"distance did not strictly decrease: {distances}"
