"""Simulated fleet for the push/alert path.

There is no public real-time AIS feed for Indian small boats (see CLAUDE.md's "Do not"
list), so the demo fleet the push loop tracks is synthetic by construction. Every
:class:`~foreshore.models.VesselState` this module hands out carries ``is_simulated=True``
— that flag is never optional-away, because presenting a simulated position as a real one
would not survive a single question from a judge.

Two of :func:`default_fleet`'s eight boats carry scripted intent rather than a random
heading, so that ``push/loop.py`` (built on top of this module) has a boat that
reliably demonstrates each alert path in a short demo window:

* one boat is aimed at the nearest point on the ``IMBL_HISTORIC_WATERS`` line, to
  demonstrate a firing ``IMBL_HISTORIC_WATERS`` warning;
* one boat is aimed out to open water, to demonstrate a hazard-cell alert once a hazard
  polygon exists there (dynamic/live hazard geometry is out of scope for this module).

:func:`advance` is the pure dead-reckoning step the push loop calls on a timer: it never
mutates its input, so the caller's own copy of a :class:`VesselState` stays valid across
a tick.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import timedelta

from ..config import Port, RegionConfig, load_region, load_vessels
from ..models import VesselState, bearing_deg, project_position, utcnow
from ..store.vectors import VectorStore

#: Vessel class every demo boat is built as. CLAUDE.md's small-boat ceiling
#: (``config/vessels.yaml``) is defined for this class, so the demo fleet exercises the
#: same thresholds the verdict engine actually enforces.
_FLEET_VESSEL_CLASS = "small_motorised"

#: Fleet size the demo/push loop is built around — see the module docstring for why two
#: of these eight carry scripted, not random, headings.
_FLEET_SIZE = 8

#: Index (within the fleet returned by ``default_fleet``) of the boat scripted to close
#: on the IMBL_HISTORIC_WATERS line. Always the first boat at the first anchor port.
_IMBL_BOAT_INDEX = 0

#: Index of the boat scripted to head out to open water.
_OFFSHORE_BOAT_INDEX = 4


def _home_ports(region: RegionConfig) -> list[Port]:
    ports = list(region.anchor_ports[:2])
    if not ports:
        raise ValueError(
            f"region {region.region_id!r} has no anchor_ports configured; "
            "cannot build the simulated demo fleet"
        )
    # Defensive only: every shipped region config carries >= 2 anchor ports. If a future
    # region config ever supplied just one, cycle it rather than crash the demo.
    return [ports[i % len(ports)] for i in range(_FLEET_SIZE)]


def _fleet_centroid(region: RegionConfig) -> tuple[float, float]:
    """Mean position of every anchor port — a generic, region-agnostic stand-in for
    "the coastline", used only to pick a plausible outward bearing for the
    open-water demo boat. No coordinate is hardcoded; this is pure geometry over
    whatever anchor_ports the active region config supplies."""
    ports = region.anchor_ports
    lat = sum(p.lat for p in ports) / len(ports)
    lon = sum(p.lon for p in ports) / len(ports)
    return lat, lon


def default_fleet(region: RegionConfig | None = None, *, seed: int = 0) -> list[VesselState]:
    """Eight simulated :class:`VesselState` records, split across the region's first two
    anchor ports, deterministic given ``seed``.

    Uses a local :class:`random.Random` instance — never the global RNG — so the same
    ``seed`` (including the default, ``seed=0``) always produces the same fleet.
    """
    region = region or load_region()
    vclass = load_vessels().get(_FLEET_VESSEL_CLASS)
    rng = random.Random(seed)
    now = utcnow()

    home_ports = _home_ports(region)

    fleet: list[VesselState] = []
    for i, port in enumerate(home_ports):
        lat, lon = port.lat, port.lon
        if i not in (_IMBL_BOAT_INDEX, _OFFSHORE_BOAT_INDEX):
            # Small deterministic jitter so the non-scripted boats aren't literally
            # stacked on the harbour mouth. The two scripted boats keep an exact,
            # known start position so their intent computation below is unambiguous.
            lat += rng.uniform(-0.03, 0.03)
            lon += rng.uniform(-0.03, 0.03)

        speed = round(vclass.cruise_speed_kn + rng.uniform(-0.6, 0.6), 2)
        heading = round(rng.uniform(0.0, 360.0), 1)

        fleet.append(
            VesselState(
                vessel_id=f"sim-{i:02d}",
                name=f"{port.name} FB-{i + 1:02d}",
                lat=lat,
                lon=lon,
                heading_deg=heading,
                speed_kn=speed,
                vessel_class=_FLEET_VESSEL_CLASS,
                updated_at=now,
                home_port=port.name,
                crew=vclass.crew_typical,
                is_simulated=True,
            )
        )

    # -- scripted boat 1: aimed at IMBL_HISTORIC_WATERS --------------------------------
    # push/loop.py uses this boat to demonstrate a firing IMBL_HISTORIC_WATERS warning.
    if _IMBL_BOAT_INDEX < len(fleet):
        imbl_boat = fleet[_IMBL_BOAT_INDEX]
        try:
            hits = VectorStore().nearest(
                "imbl_historic_waters", imbl_boat.lat, imbl_boat.lon, n=1
            )
        except Exception:  # noqa: BLE001 — layer not fetched yet; keep the random heading
            hits = []
        if hits:
            target = hits[0]
            heading_to_line = bearing_deg(
                imbl_boat.lat, imbl_boat.lon, target.closest_lat, target.closest_lon
            )
            fleet[_IMBL_BOAT_INDEX] = replace(imbl_boat, heading_deg=heading_to_line)

    # -- scripted boat 2: aimed out to open water ---------------------------------------
    # No real hazard geometry exists yet (dynamic/live cyclone/high-wave cells are out of
    # scope for this module) — this is only a believable "further offshore" track, for
    # push/loop.py's later hazard-cell alert demo.
    if _OFFSHORE_BOAT_INDEX < len(fleet):
        offshore_boat = fleet[_OFFSHORE_BOAT_INDEX]
        centroid_lat, centroid_lon = _fleet_centroid(region)
        away_bearing = bearing_deg(
            centroid_lat, centroid_lon, offshore_boat.lat, offshore_boat.lon
        )
        elevated_speed = round(min(vclass.max_speed_kn, vclass.cruise_speed_kn * 1.2), 2)
        fleet[_OFFSHORE_BOAT_INDEX] = replace(
            offshore_boat, heading_deg=away_bearing, speed_kn=elevated_speed
        )

    return fleet


def advance(vessel: VesselState, seconds: float) -> VesselState:
    """Dead-reckon ``vessel`` forward ``seconds`` along its current heading and speed.

    Pure: returns a *new* :class:`VesselState`, never mutates ``vessel``. A stationary
    boat (``speed_kn == 0``) is a position no-op — guarded explicitly so the great-circle
    projection is never asked to place a zero-distance destination point and cannot
    produce NaN.
    """
    if vessel.speed_kn == 0:
        new_lat, new_lon = vessel.lat, vessel.lon
    else:
        new_lat, new_lon = project_position(
            vessel.lat, vessel.lon, vessel.heading_deg, vessel.speed_kn, seconds
        )
    return replace(
        vessel,
        lat=new_lat,
        lon=new_lon,
        updated_at=vessel.updated_at + timedelta(seconds=seconds),
    )


__all__ = ["advance", "default_fleet"]
