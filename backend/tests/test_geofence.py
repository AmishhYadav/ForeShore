"""Tests for ``foreshore.geofence.classes`` and ``foreshore.geofence.engine``.

Two things are under test:

1. CLAUDE.md's five geofence classes stay semantically distinct — distinct severities,
   distinct lead distances where the spec says they differ, distinct copy in both
   English and Tamil, and the 1974 historic-waters line is never conflated with the
   1976 maritime boundary even though (by design) they share the same lead distances.
2. docs/DECISIONS.md D6 — geofence ETA is sampled along the projected track against the
   fence *geometry*, not from an endpoint-distance comparison: a vessel that crosses a
   boundary and keeps going must still be reported as closing, a closure slower than
   0.25 kn is suppressed as geometric noise, and an ETA beyond four projection horizons
   is not reported as a number.

The D6 tests use a synthetic :class:`~foreshore.geofence.engine.DynamicFence` (an
in-memory hazard-style fence, never written to the static store) rather than the real
IMBL/MPA layers, so the geometry is exact and independent of which static layers happen
to be committed under ``data/static/``.
"""

from __future__ import annotations

import math
from typing import get_args

import pytest

from foreshore.config import load_geofence_config
from foreshore.geofence.classes import (
    describe_classes,
    format_copy,
    spec_for,
    title_for,
)
from foreshore.geofence.engine import DynamicFence, GeofenceEngine
from foreshore.models import GeofenceClass

#: The five semantically-distinct classes CLAUDE.md's table lists, derived from the
#: canonical type contract rather than re-typed here — HAZARD_EXCLUSION is the sixth,
#: dynamic-only class and is deliberately excluded from the "five classes" checks.
ALL_CLASSES = get_args(GeofenceClass)
STATIC_CLASSES = tuple(c for c in ALL_CLASSES if c != "HAZARD_EXCLUSION")


def test_geofence_class_contract_has_five_static_classes_plus_one_dynamic():
    assert len(ALL_CLASSES) == 6
    assert len(STATIC_CLASSES) == 5
    assert "HAZARD_EXCLUSION" in ALL_CLASSES


# --------------------------------------------------------------------------------------
# 1. The five classes are semantically distinct.
# --------------------------------------------------------------------------------------


def test_every_static_class_is_defined_in_config():
    cfg = load_geofence_config()
    for gc in STATIC_CLASSES:
        assert gc in cfg.classes, f"{gc} is missing from config/geofence.yaml"


def test_titles_are_pairwise_distinct_in_every_configured_language(region):
    cfg = load_geofence_config()
    for lang in region.languages:
        titles = [title_for(gc, lang, cfg) for gc in STATIC_CLASSES]
        for t in titles:
            assert t and t.strip(), f"empty {lang} title among {STATIC_CLASSES}"
        assert len(set(titles)) == len(titles), (
            f"two geofence classes collapsed to the same {lang} title: {titles}"
        )


def test_severities_and_lead_distances_match_claude_md_where_it_says_they_differ():
    cfg = load_geofence_config()
    specs = {gc: spec_for(gc, cfg) for gc in STATIC_CLASSES}

    assert specs["IMBL_HISTORIC_WATERS"].severity == "legal_hard"
    assert specs["IMBL_MARITIME_BOUNDARY"].severity == "legal_hard"
    assert specs["MPA"].severity == "restricted"
    assert specs["ECO_SENSITIVE"].severity == "advisory"
    assert specs["USER_DEFINED"].severity == "advisory"

    # Literal values from CLAUDE.md's geofence table.
    assert (specs["IMBL_HISTORIC_WATERS"].warn_nm, specs["IMBL_HISTORIC_WATERS"].critical_nm) == (2.0, 0.5)
    assert (specs["IMBL_MARITIME_BOUNDARY"].warn_nm, specs["IMBL_MARITIME_BOUNDARY"].critical_nm) == (2.0, 0.5)
    assert (specs["MPA"].warn_nm, specs["MPA"].critical_nm) == (1.0, 0.25)
    assert specs["ECO_SENSITIVE"].warn_nm == 0.5

    # Where CLAUDE.md says the classes differ, the lead distances must actually differ:
    # IMBL is the widest legal-hard envelope, MPA narrower, ECO_SENSITIVE narrower still.
    assert specs["IMBL_HISTORIC_WATERS"].warn_nm > specs["MPA"].warn_nm > specs["ECO_SENSITIVE"].warn_nm
    assert specs["IMBL_HISTORIC_WATERS"].critical_nm > specs["MPA"].critical_nm > specs["ECO_SENSITIVE"].critical_nm


def test_no_two_classes_collapse_to_identical_warn_copy(region):
    """A blunter, direct check that the rendered WARN-level copy is never interchangeable
    between classes, for every configured language."""
    cfg = load_geofence_config()
    for lang in region.languages:
        rendered = {
            gc: format_copy(gc, "WARN", lang, name="X", distance_nm=1.0, eta_seconds=600, cfg=cfg)
            for gc in STATIC_CLASSES
        }
        values = list(rendered.values())
        assert len(set(values)) == len(values), f"duplicate {lang} WARN copy: {rendered}"


def test_mpa_copy_clarifies_conservation_not_a_border():
    cfg = load_geofence_config()
    en_warn = format_copy(
        "MPA", "WARN", "en", name="Gulf of Mannar Marine National Park",
        distance_nm=1.0, cfg=cfg,
    )
    assert "national border" in en_warn.lower()
    assert "conservation" in en_warn.lower() or "restrictions apply" in en_warn.lower()


def test_describe_classes_legend_has_no_duplicate_classes():
    rows = describe_classes("en")
    seen = [r["geofence_class"] for r in rows]
    assert len(seen) == len(set(seen))
    for gc in STATIC_CLASSES:
        assert gc in seen


# --------------------------------------------------------------------------------------
# 2. IMBL_HISTORIC_WATERS (1974) is not the same object or copy as
#    IMBL_MARITIME_BOUNDARY (1976), even though they share the same numeric envelope.
# --------------------------------------------------------------------------------------


def test_imbl_historic_waters_and_maritime_boundary_are_distinct_treaty_regimes(region):
    cfg = load_geofence_config()
    hist = spec_for("IMBL_HISTORIC_WATERS", cfg)
    mar = spec_for("IMBL_MARITIME_BOUNDARY", cfg)

    assert hist != mar
    assert hist.geofence_class != mar.geofence_class

    # By design (CLAUDE.md's table) the two share the same lead-distance envelope — that
    # sameness is intentional and is asserted here, not assumed away.
    assert (hist.warn_nm, hist.critical_nm) == (mar.warn_nm, mar.critical_nm) == (2.0, 0.5)

    # What must never be shared is the copy: it names a specific, different treaty.
    hist_title_en = title_for("IMBL_HISTORIC_WATERS", "en", cfg)
    mar_title_en = title_for("IMBL_MARITIME_BOUNDARY", "en", cfg)
    assert hist_title_en != mar_title_en
    assert "1974" in hist_title_en and "1974" not in mar_title_en
    assert "1976" in mar_title_en and "1976" not in hist_title_en

    for lang in region.languages:
        for level in ("WARN", "CRITICAL", "BREACH"):
            hist_copy = format_copy(
                "IMBL_HISTORIC_WATERS", level, lang, name="X", distance_nm=1.0, eta_seconds=600, cfg=cfg
            )
            mar_copy = format_copy(
                "IMBL_MARITIME_BOUNDARY", level, lang, name="X", distance_nm=1.0, eta_seconds=600, cfg=cfg
            )
            assert hist_copy != mar_copy, f"{lang}/{level} copy is identical for 1974 vs 1976 boundary"


# --------------------------------------------------------------------------------------
# 3. D6 — proximity/ETA is sampled along the track against the fence geometry.
# --------------------------------------------------------------------------------------


def _nm_to_deg_lon(nm: float, at_lat_deg: float) -> float:
    """Rough nm -> degrees-of-longitude conversion at a given latitude, good enough to
    place a synthetic test fence a known approximate distance away; the engine itself
    always measures the real answer with the exact haversine formula, so test
    assertions below use a generous tolerance rather than exact equality."""
    return (nm / 60.0) / max(math.cos(math.radians(at_lat_deg)), 0.05)


def _meridian_fence(lon_deg: float, lat_span: tuple[float, float] = (7.5, 11.0)) -> DynamicFence:
    lat_min, lat_max = lat_span
    return DynamicFence(
        fence_id="test_meridian_fence",
        name="Test boundary",
        geometry={"type": "LineString", "coordinates": [[lon_deg, lat_min], [lon_deg, lat_max]]},
        # HAZARD_EXCLUSION is DynamicFence's default class -- used here purely as a
        # convenient dynamic-only carrier for a synthetic geometry, not as a claim about
        # what kind of real-world fence this is.
    )


def _only_result(engine: GeofenceEngine, lat: float, lon: float, heading_deg: float, speed_kn: float):
    results = engine.check(
        lat, lon, heading_deg=heading_deg, speed_kn=speed_kn,
        classes=["HAZARD_EXCLUSION"], include_info=True,
    )
    assert len(results) == 1, "expected exactly the one injected synthetic fence"
    return results[0]


def test_eta_reports_closing_for_a_vessel_that_crosses_and_keeps_going(region):
    """The obvious-but-wrong implementation (endpoint distance only) would say a boat
    that crosses the line and keeps going is "not closing", because it ends further
    from the line than it started. D6 requires the track to be sampled instead."""
    engine = GeofenceEngine()
    lat0, fence_lon = region.centre
    start_lon = fence_lon - _nm_to_deg_lon(1.0, lat0)
    engine.add_dynamic(_meridian_fence(fence_lon))

    # Due east at 4 kn: crosses a line 1 nm away after ~15 minutes, then keeps going.
    prox = _only_result(engine, lat0, start_lon, heading_deg=90.0, speed_kn=4.0)

    assert prox.distance_nm == pytest.approx(1.0, abs=0.05)
    assert prox.eta_seconds is not None, "a track that crosses the fence must report a closing ETA"
    assert 700 <= prox.eta_seconds <= 1100  # ~900 s expected, generous tolerance


def test_eta_is_suppressed_below_the_minimum_closing_rate(region):
    """A boat running almost parallel to the boundary closes on it only through the
    geometry of the meridians converging; D6 says that must not be reported as an ETA."""
    engine = GeofenceEngine()
    lat0, fence_lon = region.centre
    start_lon = fence_lon - _nm_to_deg_lon(5.0, lat0)
    engine.add_dynamic(_meridian_fence(fence_lon))

    # Heading mostly north with only a 5-degree eastward component, at a crawl (0.5 kn):
    # the closing component of that motion is well under the 0.25 kn suppression floor.
    prox = _only_result(engine, lat0, start_lon, heading_deg=5.0, speed_kn=0.5)

    assert prox.distance_nm == pytest.approx(5.0, abs=0.1)
    assert prox.eta_seconds is None


def test_eta_beyond_four_projection_horizons_is_not_reported(region):
    """Closing fast enough to clear the minimum-closing-rate floor, but so far out that
    the resulting ETA is not an actionable number -- D6 says suppress it rather than
    show a large, unhelpful figure."""
    engine = GeofenceEngine()
    lat0, fence_lon = region.centre
    start_lon = fence_lon - _nm_to_deg_lon(20.0, lat0)
    engine.add_dynamic(_meridian_fence(fence_lon))

    prox = _only_result(engine, lat0, start_lon, heading_deg=45.0, speed_kn=1.0)

    assert prox.distance_nm == pytest.approx(20.0, abs=0.2)
    assert prox.eta_seconds is None


def test_stationary_or_undirected_vessel_gets_no_eta(region):
    """No heading/speed at all must degrade to "no ETA", never to a crash or a 0."""
    engine = GeofenceEngine()
    lat0, fence_lon = region.centre
    start_lon = fence_lon - _nm_to_deg_lon(1.0, lat0)
    engine.add_dynamic(_meridian_fence(fence_lon))

    results = engine.check(lat0, start_lon, classes=["HAZARD_EXCLUSION"], include_info=True)
    assert len(results) == 1
    assert results[0].eta_seconds is None
