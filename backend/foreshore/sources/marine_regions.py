"""Marine Regions / VLIZ maritime-boundary adapter — the India / Sri Lanka IMBL.

Single keyless WFS 1.0.0 ``GetFeature``, probed live on 2026-08-31 before writing any
parser: ``https://geo.vliz.be/geoserver/MarineRegions/wfs``, ``typeName=
MarineRegions:eez_boundaries``, ``outputFormat=application/json``, filtered with
``CQL_FILTER=line_name LIKE '%Sri Lanka%'`` (the pattern lives in region config as
``imbl_line_filter`` — never hand-built here). Geometry is ``MultiLineString`` in every
case, so every :class:`~foreshore.models.Provenance` emitted carries
``spatial_resolution_m=None``.

Real ``properties`` keys found on the wire: ``line_id, line_name, line_type, mrgid_sov1,
mrgid_ter1, territory1, sovereign1, mrgid_ter2, territory2, mrgid_sov2, sovereign2,
mrgid_eez1, eez1, mrgid_eez2, eez2, source1, url1, source2, url2, source3, url3, origin,
doc_date, mrgid_jreg, joint_reg, length_km``.

The name filter alone returns **7** features for this coast, not 4: the four treaty
boundary segments this module cares about (``line_type == "Treaty"``, ``line_id`` 1306 /
1307 / 1310 / 1311) plus three lines VLIZ also tags with "Sri Lanka" that are not
negotiated boundaries at all — a 200 nautical-mile EEZ-limit buffer (``line_id 3655``,
``line_type "200 NM"``) and two short EEZ "connection lines" stitching baselines
together (``line_id`` 3848, 3937, ``line_type "Connection line"``). ``segments()`` keeps
only ``line_type == "Treaty"``; that value is MarineRegions' own global vocabulary for
"this line came from a bilateral agreement," not a Tamil-Nadu-specific hardcode, so
filtering on it does not violate the region-config-only rule. ``doc_date`` arrives as
e.g. ``"1974-06-28Z"`` (a date, zulu-suffixed, not a real timestamp) and is stripped of
the trailing ``Z`` for ``treaty_date``. ``source1`` carries the full bilateral-agreement
title and is used verbatim as ``treaty`` — this is the answer to "where did your
maritime boundary come from?", straight from the data, no hand-digitised coordinates
anywhere in this module.

Class assignment comes from region config (``imbl_historic_waters_line_ids`` = ``[1306]``,
``imbl_maritime_boundary_line_ids`` = ``[1307, 1310, 1311]`` for ``palk_bay_gom``), never
from ids hardcoded here. Any ``Treaty`` segment whose ``line_id`` is not in either
configured list still gets returned — never silently dropped — but falls back to
``IMBL_MARITIME_BOUNDARY`` with a note recorded on the segment, since collapsing an
unrecognised boundary into "doesn't exist" is worse than a conservative default.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..models import Observation
from .base import FetchResult, Source

VLIZ_WFS = "https://geo.vliz.be/geoserver/MarineRegions/wfs"
TYPE_NAME = "MarineRegions:eez_boundaries"

DEFAULT_LINE_FILTER = "%Sri Lanka%"
HISTORIC_CLASS = "IMBL_HISTORIC_WATERS"
MARITIME_CLASS = "IMBL_MARITIME_BOUNDARY"


def _features(raw: FetchResult) -> list[dict[str, Any]]:
    payload = raw.payload
    if not isinstance(payload, dict):
        return []
    feats = payload.get("features")
    return list(feats) if isinstance(feats, list) else []


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _strip_zulu(value: Any) -> str | None:
    s = _clean_str(value)
    if s is None:
        return None
    return s[:-1] if s.endswith("Z") else s


@dataclass(frozen=True)
class BoundarySegment:
    line_id: int
    line_name: str
    geofence_class: str
    treaty: str | None
    treaty_date: str | None
    geometry: dict[str, Any]
    properties: dict[str, Any]

    def to_geojson_feature(self) -> dict[str, Any]:
        return {"type": "Feature", "geometry": self.geometry, "properties": self.properties}


def _classify(line_id: int, historic_ids: set[int], maritime_ids: set[int]) -> tuple[str, str | None]:
    if line_id in historic_ids:
        return HISTORIC_CLASS, None
    if line_id in maritime_ids:
        return MARITIME_CLASS, None
    return (
        MARITIME_CLASS,
        f"line_id {line_id} not present in either configured IMBL id list; "
        f"defaulted to {MARITIME_CLASS}",
    )


def _id_set(values: Any) -> set[int]:
    if not values:
        return set()
    out = set()
    for v in values:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            continue
    return out


class MarineRegionsIMBL(Source):
    """VLIZ Marine Regions treaty maritime-boundary segments for the configured region."""

    source_id = "marine_regions_imbl"
    source_name = "Marine Regions / VLIZ maritime boundaries"
    authority = "VLIZ"
    validity = timedelta(days=365)  # a treaty boundary does not go stale
    cache_ttl_s = 86400.0
    spatial_resolution_m = None  # vector line features

    # -- transport -------------------------------------------------------------------

    def _wfs_get(self) -> FetchResult:
        pattern = self.region.source("imbl_line_filter") or DEFAULT_LINE_FILTER
        params = {
            "service": "WFS",
            "version": "1.0.0",
            "request": "GetFeature",
            "typeName": TYPE_NAME,
            "outputFormat": "application/json",
            "CQL_FILTER": f"line_name LIKE '{pattern}'",
        }
        return self.get(VLIZ_WFS, params=params, as_json=True)

    # -- typed outputs -----------------------------------------------------------------

    def segments(self) -> tuple[list[BoundarySegment], FetchResult]:
        raw = self._wfs_get()
        historic_ids = _id_set(self.region.source("imbl_historic_waters_line_ids"))
        maritime_ids = _id_set(self.region.source("imbl_maritime_boundary_line_ids"))
        out: list[BoundarySegment] = []
        for feat in _features(raw):
            props = feat.get("properties") or {}
            if _clean_str(props.get("line_type")) != "Treaty":
                continue  # 200 NM limit / connection lines — not negotiated boundaries
            raw_id = props.get("line_id")
            if raw_id is None:
                continue
            try:
                line_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            geofence_class, note = _classify(line_id, historic_ids, maritime_ids)
            treaty = _clean_str(props.get("source1")) or _clean_str(props.get("source2"))
            treaty_date = _strip_zulu(props.get("doc_date"))
            merged = dict(props)
            merged["geofence_class"] = geofence_class
            merged["treaty"] = treaty
            merged["treaty_date"] = treaty_date
            if note:
                merged["geofence_class_note"] = note
            out.append(
                BoundarySegment(
                    line_id=line_id,
                    line_name=_clean_str(props.get("line_name")) or "",
                    geofence_class=geofence_class,
                    treaty=treaty,
                    treaty_date=treaty_date,
                    geometry=feat.get("geometry") or {},
                    properties=merged,
                )
            )
        return out, raw

    def as_geojson(self) -> dict[str, Any]:
        segs, _raw = self.segments()
        return {"type": "FeatureCollection", "features": [s.to_geojson_feature() for s in segs]}

    # -- generic Source contract --------------------------------------------------------

    def parse(self, raw: FetchResult, **kw: Any) -> list[Observation]:
        historic_ids = _id_set(self.region.source("imbl_historic_waters_line_ids"))
        maritime_ids = _id_set(self.region.source("imbl_maritime_boundary_line_ids"))
        lat, lon = self.region.centre
        prov = self.provenance(
            raw,
            valid_from=raw.acquired_at,
            is_derived=False,
            notes=(
                "Marine Regions / VLIZ India-Sri Lanka maritime boundary segments "
                "(MarineRegions:eez_boundaries), treaty lines only."
            ),
        )
        out: list[Observation] = []
        for feat in _features(raw):
            props = feat.get("properties") or {}
            if _clean_str(props.get("line_type")) != "Treaty":
                continue
            raw_id = props.get("line_id")
            if raw_id is None:
                continue
            try:
                line_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            geofence_class, note = _classify(line_id, historic_ids, maritime_ids)
            treaty = _clean_str(props.get("source1")) or _clean_str(props.get("source2"))
            treaty_date = _strip_zulu(props.get("doc_date"))
            out.append(
                self.observe(
                    "maritime_boundary_segment",
                    line_id,
                    "id",
                    lat,
                    lon,
                    valid_time=raw.acquired_at,
                    provenance=prov,
                    line_name=_clean_str(props.get("line_name")),
                    geofence_class=geofence_class,
                    treaty=treaty,
                    treaty_date=treaty_date,
                    length_km=props.get("length_km"),
                    geometry=feat.get("geometry"),
                    note=note,
                )
            )
        return out

    def fetch(self, **kwargs: Any) -> FetchResult:
        return self._wfs_get()

    def health(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            segs, raw = self.segments()
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {
                "source_id": self.source_id,
                "ok": True,
                "count": len(segs),
                "latency_ms": latency_ms,
                "issued_at": raw.acquired_at.isoformat(),
                "freshness": None,
                "resolution_m": self.spatial_resolution_m,
                "error": None,
                "historic_waters_count": sum(1 for s in segs if s.geofence_class == HISTORIC_CLASS),
                "maritime_boundary_count": sum(1 for s in segs if s.geofence_class == MARITIME_CLASS),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "source_id": self.source_id,
                "ok": False,
                "count": 0,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "issued_at": None,
                "freshness": None,
                "resolution_m": self.spatial_resolution_m,
                "error": f"{type(exc).__name__}: {exc}",
                "historic_waters_count": None,
                "maritime_boundary_count": None,
            }


__all__ = ["VLIZ_WFS", "BoundarySegment", "MarineRegionsIMBL", "HISTORIC_CLASS", "MARITIME_CLASS"]
