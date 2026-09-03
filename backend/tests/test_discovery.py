"""Tests for tool 16 — ``list_available_data`` (the ``MarineDataDiscovery`` specialist's
sole tool).

Real source count is **nine**, not eight — see ``foreshore/tools/discovery.py``'s module
docstring for the justification: ``sources/openmeteo.py`` defines two distinct
:class:`~foreshore.sources.base.Source` subclasses (``OpenMeteoMarine``,
``OpenMeteoForecast``, different ``source_id``/``spatial_resolution_m``/``validity``),
and ``scripts/healthcheck.py``'s single combined "openmeteo" report row is a
report-formatting convenience over there, not adapter identity. A coverage table's job
is one row per real, distinct ``source_id`` — so this tool probes all nine:
``imd_coastal_bulletin``, ``imd_geoserver``, ``incois_wfs``, ``incois_osf``,
``incois_argo``, ``openmeteo_marine``, ``openmeteo_forecast``, ``gdacs_tc``,
``marine_regions_imbl``.

``data/fixtures/`` is frozen from real live traffic by ``scripts/freeze_fixtures.py``
(see that script's own docstring), so under ``FORESHORE_MODE=fixture`` every adapter's
``.health()`` now replays a real snapshot rather than raising ``FixtureMissing`` — this
is what makes the venue-wifi-off demo path actually work, not just theoretically exist.
``test_returns_ok_true_with_nine_rows_each_carrying_a_real_source_id`` asserts that
success directly. The "gap is a finding, not a failure" framing this tool is built
around is still exercised structurally by
``test_one_adapter_health_raising_does_not_sink_the_other_rows`` below (a monkeypatched
failure), not by depending on the fixture store being empty — these tests assert the
tool's own contract (shape, isolation, provenance, mode discipline), never a live
source's current reachability.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from foreshore.config import is_fixture
from foreshore.models import Observation, Provenance, ToolResult
from foreshore.tools import registry
from foreshore.tools.discovery import list_available_data

EXPECTED_SOURCE_IDS = {
    "imd_coastal_bulletin",
    "imd_geoserver",
    "incois_wfs",
    "incois_osf",
    "incois_argo",
    "openmeteo_marine",
    "openmeteo_forecast",
    "gdacs_tc",
    "marine_regions_imbl",
}


def test_tool_is_registered_for_marine_data_discovery():
    spec = registry.get("list_available_data")
    assert spec.number == 16
    assert spec.specialists == ("MarineDataDiscovery",)
    assert set(spec.reads_sources) == EXPECTED_SOURCE_IDS


def test_returns_ok_true_with_nine_rows_each_carrying_a_real_source_id():
    """Contract point 1: fixture mode, ok=True, one row per real distinct adapter
    (nine — see module docstring), every row's source_id non-empty."""
    assert is_fixture(), "this test must run under FORESHORE_MODE=fixture"

    result = list_available_data()

    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.tool == "list_available_data"

    rows = result.payload["sources"]
    assert len(rows) == 9, f"expected 9 source rows, got {len(rows)}: {[r['source_id'] for r in rows]}"

    seen_ids = {row["source_id"] for row in rows}
    assert seen_ids == EXPECTED_SOURCE_IDS
    for row in rows:
        assert isinstance(row["source_id"], str) and row["source_id"], f"blank source_id: {row}"

    # data/fixtures/ is frozen from real live traffic (scripts/freeze_fixtures.py), so
    # every adapter's .health() replays real data here and reports ok=True — this is
    # the whole point of freezing fixtures: the network-off path is not just reachable,
    # it is honestly healthy. A source that were genuinely unreachable even live (or
    # whose fixture went stale) would still show up here as ok=False, per the
    # "gap is a finding, not a failure" framing — see the isolation test below for that
    # path exercised directly, rather than depending on an empty fixture store.
    assert all(row["ok"] is True for row in rows), [r for r in rows if not r["ok"]]
    # list_available_data's summary only enumerates source_ids by name on the
    # unreachable/gap branch (see tools/discovery.py) — with every row healthy it is
    # the short "N of N data sources reachable." form, so assert that instead of a
    # per-row name-in-summary check that only applies to the failure branch.
    assert result.summary == "9 of 9 data sources reachable."


def test_every_observation_carries_a_real_provenance():
    """Contract point 2: structural check — no bare numbers, every Observation's
    provenance has a real source_id and a real acquired_at datetime."""
    result = list_available_data()

    assert len(result.observations) == 9
    for obs in result.observations:
        assert isinstance(obs, Observation)
        assert isinstance(obs.provenance, Provenance)
        assert isinstance(obs.provenance.source_id, str) and obs.provenance.source_id
        assert isinstance(obs.provenance.acquired_at, datetime)
        # Every quantitative claim traces to a Provenance with a source and an
        # acquisition timestamp — invariant 3 (CLAUDE.md), checked per-observation here.
        assert obs.provenance.source_id in EXPECTED_SOURCE_IDS
        assert obs.variable == "data_coverage"


def test_one_adapter_health_raising_does_not_sink_the_other_rows(monkeypatch):
    """Contract point 3: isolation. Monkeypatch one adapter's .health() itself to raise
    (not just its internal fetch — a raise straight out of health()), and confirm the
    tool still returns ok=True with all nine rows, one now showing the failure, rather
    than the whole call raising or that source silently vanishing from payload."""
    from foreshore.sources.gdacs import GDACSCyclones

    def _boom(self):
        raise RuntimeError("simulated total health() failure for gdacs_tc")

    monkeypatch.setattr(GDACSCyclones, "health", _boom)

    result = list_available_data()

    assert result.ok is True
    rows = result.payload["sources"]
    assert len(rows) == 9
    assert {row["source_id"] for row in rows} == EXPECTED_SOURCE_IDS

    gdacs_row = next(row for row in rows if row["source_id"] == "gdacs_tc")
    assert gdacs_row["ok"] is False
    assert "simulated total health() failure" in (gdacs_row["error"] or "")

    # The other eight rows are unaffected by gdacs_tc's monkeypatched failure.
    other_ids = EXPECTED_SOURCE_IDS - {"gdacs_tc"}
    assert {row["source_id"] for row in rows if row["source_id"] != "gdacs_tc"} == other_ids


def test_foreshore_mode_is_never_mutated_by_this_tool():
    """Contract point 4 / standing invariant: unlike scripts/healthcheck.py (which
    forces FORESHORE_MODE=live), this tool must behave identically under whatever mode
    the process is already running in and must never flip it."""
    before = os.environ.get("FORESHORE_MODE")
    assert before == "fixture"  # conftest.py's session-wide guarantee

    list_available_data()

    after = os.environ.get("FORESHORE_MODE")
    assert after == before
    assert is_fixture()


def test_callable_through_the_shared_registry_too():
    """Sanity: the tool is reachable the same way every other tool is called (not just
    by importing the function directly)."""
    result = registry.call("list_available_data", {})
    assert result.ok is True
    assert len(result.payload["sources"]) == 9
