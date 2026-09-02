"""Shared fixtures for the FORESHORE test suite.

Two things this file guarantees before any test module is collected:

1. ``FORESHORE_MODE=fixture`` for the whole session, so no adapter opens a socket
   (:class:`foreshore.sources.base.Source` checks this via ``config.is_fixture()`` and
   replays frozen snapshots instead of fetching — see that module's docstring).
2. ``FORESHORE_PG_DSN`` is unset, so :class:`foreshore.store.vectors.VectorStore` never
   attempts a live PostGIS connection and always falls back to the file backend.

Both are set at *import* time (module level, not inside a fixture) so they are in force
before any test module's own top-level imports run — a test file that does
``from foreshore.tools import registry`` at collection time must not race the mode.
"""

from __future__ import annotations

import os

# -- force fixture mode / no network, before anything else imports foreshore -----------
os.environ["FORESHORE_MODE"] = "fixture"
os.environ.pop("FORESHORE_PG_DSN", None)

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

# backend/foreshore is importable as "foreshore" once backend/ is on sys.path. Tests are
# run from the repo root (`pytest backend/tests`), so add backend/ defensively rather
# than relying on an installed package or a particular invocation directory.
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from foreshore import config as foreshore_config  # noqa: E402
from foreshore.config import VesselCatalogue, VesselClass, load_region, load_vessels  # noqa: E402
from foreshore.models import Observation, Provenance, UTC, utcnow  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _enforce_fixture_mode() -> None:
    """Belt-and-braces: fail loudly if anything has flipped the mode back to live."""
    assert foreshore_config.is_fixture(), (
        "FORESHORE_MODE must be 'fixture' for the whole test session — a test or an "
        "import somewhere flipped it back to 'live', which would let a source adapter "
        "open a socket."
    )
    assert os.environ.get("FORESHORE_PG_DSN") is None, (
        "FORESHORE_PG_DSN must stay unset in tests so VectorStore never dials PostGIS."
    )


# --------------------------------------------------------------------------------------
# Region / vessel config — loaded from the same files the application loads, never
# duplicated as literals in test logic.
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def region():
    """The default region config (``FORESHORE_REGION`` unset -> ``palk_bay_gom``),
    loaded via the same ``config.load_region`` the application uses."""
    return load_region()


@pytest.fixture(scope="session")
def vessel_catalogue() -> VesselCatalogue:
    """The full vessel catalogue from ``config/vessels.yaml``."""
    return load_vessels()


@pytest.fixture(scope="session")
def vessel(vessel_catalogue: VesselCatalogue) -> VesselClass:
    """The catalogue's default vessel class (``small_motorised``)."""
    return vessel_catalogue.get(None)


# --------------------------------------------------------------------------------------
# Observation / Provenance builders
# --------------------------------------------------------------------------------------

_DEFAULT_NOW = utcnow()


@pytest.fixture
def make_provenance():
    """Factory fixture: ``make_provenance(**overrides) -> Provenance``.

    Defaults describe a plausible, fully-populated live record (11 km INCOIS OSF-style
    resolution, a 12 h validity window centred on "now") so a test only has to override
    the field(s) it actually cares about.
    """

    def _make(**overrides: Any) -> Provenance:
        now = overrides.pop("_now", _DEFAULT_NOW)
        defaults: dict[str, Any] = {
            "source_id": "test_source",
            "source_name": "Test Source Adapter",
            "authority": "INCOIS",
            "url": "https://example.test/source",
            "acquired_at": now,
            "issued_at": now,
            "valid_from": now - timedelta(hours=1),
            "valid_to": now + timedelta(hours=11),
            "spatial_resolution_m": 11_000.0,
            "is_derived": False,
        }
        defaults.update(overrides)
        return Provenance(**defaults)

    return _make


@pytest.fixture
def make_observation(make_provenance, region):
    """Factory fixture: ``make_observation(**overrides) -> Observation``.

    Defaults to a significant-wave-height reading at the region's centre, carrying a
    freshly-built :class:`Provenance` unless the caller supplies its own.
    """

    def _make(**overrides: Any) -> Observation:
        lat0, lon0 = region.centre
        defaults: dict[str, Any] = {
            "variable": "significant_wave_height",
            "value": 1.0,
            "unit": "m",
            "lat": lat0,
            "lon": lon0,
            "valid_time": _DEFAULT_NOW,
            "qualifiers": {},
        }
        defaults.update(overrides)
        if "provenance" not in defaults:
            defaults["provenance"] = make_provenance()
        return Observation(**defaults)

    return _make


@pytest.fixture
def fixed_now() -> datetime:
    """A stable, timezone-aware reference instant for time-sensitive tests (bulletin
    validity, ceiling expiry) so assertions do not depend on wall-clock drift between
    building an input and evaluating it."""
    return _DEFAULT_NOW.replace(tzinfo=UTC)
