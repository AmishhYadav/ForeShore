"""INCOIS GeoServer (WFS) adapter.

Six INCOIS GeoServer workspaces, each exposed at its own ``<workspace>/ows`` path under
``https://incois.gov.in/geoserver``. All are WFS 1.0.0, keyless, GeoJSON-capable, and all
403 without a browser ``User-Agent`` + ``Referer`` — ``Source.get`` already supplies both.

Probed live on 2026-08-30 with ``maxFeatures=2/3`` before writing any parser, per the
brief. Findings, so nobody has to re-probe:

Workspace paths — every one of the six below answered ``200`` directly at
``https://incois.gov.in/geoserver/<workspace>/ows``. No 404s were seen, so the documented
generic-``/ows`` fallback (below) was never actually exercised; a direct probe of the bare
``https://incois.gov.in/geoserver/ows`` (no workspace segment) returned a mod_security
``403 Forbidden`` even for a plain ``GetCapabilities``, so that fallback is best-effort
only — kept because the brief asks for it, not because it is known to work. The service
did serve one transient ``503 Service Unavailable`` burst across all six layers during
probing; a short retry made it disappear. ``Source.get`` already retries+backs off and
falls back to the last good snapshot on persistent failure, so this is handled.

Real attribute names found (``GetFeature`` GeoJSON ``properties``):

* ``PFZ_Automation:pfzlines`` (MultiLineString) — ``category, SECTORBOUN, SECTORBO_1,
  SECTORNAME, Julian_day, Sno, Year, UID, Length``. ``SECTORNAME`` was empty on every
  sampled feature. ``Julian_day`` arrives as a **string** ("242"), ``Year`` as an int.
  ``UID`` is a float concatenation of Year+Julian_day+Sno (e.g. ``2026242037.0``).
  50 features nationally today; exactly 1 fell inside the Palk Bay/GoM bbox
  (``BBOX=78.0,8.0,80.6,10.9``) — ``UID=2026242037``, i.e. issued for day-of-year 242 of
  2026, which is 2026-08-30, today. This is the OFFICIAL INCOIS PFZ advisory line.
* ``PFZ_Sectors:sector_new`` (MultiPolygon) — ``SDE_SECTOR, PERIMETER, SBOUND_,
  SBOUND_ID, SECTORNAME, SEC_ID, SHAPE_AREA, SHAPE_LEN``. ``SEC_ID='SEC006'`` →
  ``SECTORNAME='SOUTH TAMILNADU'``, confirming the region config value. 14 sectors
  nationally.
* ``PFZ_LandingCentres:LandingCenters_29Apr2024`` (Point) — ``OBJECTID, SECTOR_NAM,
  SECTOR_ID, DIST_NAME, LC_NAME, LC_UNIQUE_, LONGITUDE, LATITUDE, FORECAST_I, UPDATED_DA,
  FORECAST_D, VALIDITY_D, DIRECTION, BEARING, DISTANCE_F, DISTANCE_T, DEPTH_FROM,
  DEPTH_TO, ..., STATUS, MARINE_FIS``. 1223 features nationally, 137 inside the Palk
  Bay/GoM bbox. There is **no separate state field** — ``SECTOR_NAM`` (e.g.
  ``"SOUTH TAMILNADU"``, ``"MAHARASHTRA"``) is the closest honest proxy and is what
  :class:`LandingCentre`.state uses. Name = ``LC_NAME``, district = ``DIST_NAME``.
* ``MHW:CORAL_REEF_DISS`` / ``MHW:SEAGRASS_ZONE_DISS`` (MultiPolygon) — each is a single
  nationally-**dissolved** feature (``totalFeatures: 1``) with a near-empty properties
  block (``{"CoralReef": "CoralReef"}`` / ``{"NAME": "Seagrass"}``). There is no
  per-reef/per-meadow granularity in this layer, and a server-side BBOX cannot reduce a
  1-feature dissolved layer below 1 feature — ``bbox_filter`` is applied locally as a
  defensive check, not because it narrows anything here.
* ``MHW:MANGROVE_ZONE_DISS`` — **flaky today**: repeated probes returned a mix of
  ``503``, ``502`` and read-timeouts even at ``maxFeatures=2``, never a clean 200 in
  several attempts across ~2 minutes of testing. Implemented identically to coral/
  seagrass; a failure here is a real, surfaced `SourceError` — never silently swallowed
  into a fake empty success (invariant: staleness/failure is surfaced, never hidden).
* ``ABIS:HABSectors`` (MultiLineString) — properties are just ``Id, Location``. 4
  sectors nationally; ``Location="Gulf of Manmar (GoM)"`` (``Id=0``) is an exact string
  match for this region's ``incois_hab_sector`` config value. Importantly, **this layer
  carries no bloom-active/severity attribute at all** — it is a static sector
  membership list, not a live HAB alert feed. ``parse_hab`` therefore emits the sector
  name as the categorical value and says explicitly, in a qualifier, that no live status
  field exists — it does not invent one.
* ``PFZ_Bathymetry:bathymetry`` (MultiLineString, depth contours) — ``FNODE_, TNODE_,
  LPOLY_, RPOLY_, LENGTH, BATHMETRY_, BATHMETRY1, BATHMETR_1, BATHYLABEL``. 1776 features
  nationally. ``BATHMETRY1``/``BATHMETR_1`` carry the contour depth value (duplicated
  across both fields in every sample); ``BATHYLABEL`` was blank on every sample seen.

All six layers are vector features, so per the spatial-resolution convention every
:class:`~foreshore.models.Provenance` emitted here carries ``spatial_resolution_m=None``.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qs, urlsplit

from ..models import UTC, Observation, Provenance, bearing_deg, haversine_nm
from .base import FetchResult, Source, SourceError, bbox_filter

INCOIS_GEOSERVER = "https://incois.gov.in/geoserver"

#: layer name -> (workspace, fully-qualified typeName)
_LAYERS: dict[str, tuple[str, str]] = {
    "pfz_lines": ("PFZ_Automation", "PFZ_Automation:pfzlines"),
    "pfz_sectors": ("PFZ_Sectors", "PFZ_Sectors:sector_new"),
    "landing_centres": ("PFZ_LandingCentres", "PFZ_LandingCentres:LandingCenters_29Apr2024"),
    "eco_coral": ("MHW", "MHW:CORAL_REEF_DISS"),
    "eco_seagrass": ("MHW", "MHW:SEAGRASS_ZONE_DISS"),
    "eco_mangrove": ("MHW", "MHW:MANGROVE_ZONE_DISS"),
    "hab_sectors": ("ABIS", "ABIS:HABSectors"),
    "bathymetry": ("PFZ_Bathymetry", "PFZ_Bathymetry:bathymetry"),
}

_ECO_TYPE_NAMES = {
    "coral": "MHW:CORAL_REEF_DISS",
    "seagrass": "MHW:SEAGRASS_ZONE_DISS",
    "mangrove": "MHW:MANGROVE_ZONE_DISS",
}


# --------------------------------------------------------------------------------------
# small local helpers — no dependency on base.py internals
# --------------------------------------------------------------------------------------


def _features(raw: FetchResult) -> list[dict[str, Any]]:
    payload = raw.payload
    if not isinstance(payload, dict):
        return []
    feats = payload.get("features")
    return list(feats) if isinstance(feats, list) else []


def _walk_coords(node: Any) -> Iterable[tuple[float, float]]:
    """Yield every (lon, lat) vertex pair in a GeoJSON ``coordinates`` tree."""
    if node is None:
        return
    if isinstance(node, (list, tuple)):
        if len(node) >= 2 and isinstance(node[0], (int, float)) and isinstance(node[1], (int, float)):
            yield float(node[0]), float(node[1])
            return
        for child in node:
            yield from _walk_coords(child)


def _iter_vertices(geometry: dict[str, Any] | None) -> Iterable[tuple[float, float]]:
    if not geometry:
        return
    yield from _walk_coords(geometry.get("coordinates"))


def _type_name_of(url: str) -> str | None:
    qs = parse_qs(urlsplit(url).query)
    for key in ("typeName", "typename", "TYPENAME"):
        if key in qs and qs[key]:
            return qs[key][0]
    return None


def _advisory_date(year: Any, julian_day: Any) -> datetime | None:
    """``Year`` + day-of-year ``Julian_day`` -> a UTC midnight date. Both arrive loosely
    typed off the wire (``Year`` an int, ``Julian_day`` a numeric string); missing or
    unparsable inputs yield ``None`` rather than a guessed date."""
    if year is None or julian_day is None:
        return None
    try:
        y = int(year)
        j = int(str(julian_day).strip())
    except (TypeError, ValueError):
        return None
    if j < 1:
        return None
    return datetime(y, 1, 1, tzinfo=UTC) + timedelta(days=j - 1)


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LandingCentre:
    name: str
    district: str | None
    state: str | None
    lat: float
    lon: float
    distance_nm: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "district": self.district,
            "state": self.state,
            "lat": self.lat,
            "lon": self.lon,
            "distance_nm": round(self.distance_nm, 3) if self.distance_nm is not None else None,
        }


def _landing_centre_from_feature(feat: dict[str, Any]) -> LandingCentre | None:
    props = feat.get("properties") or {}
    name = _clean_str(props.get("LC_NAME"))
    if not name:
        return None  # the DO_NOT_ADVISE handoff must never receive an unnamed centre
    geom = feat.get("geometry") or {}
    coords = geom.get("coordinates")
    lon: float | None = None
    lat: float | None = None
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        lon, lat = float(coords[0]), float(coords[1])
    if lon is None or lat is None:
        raw_lon, raw_lat = props.get("LONGITUDE"), props.get("LATITUDE")
        if raw_lon is None or raw_lat is None:
            return None
        lon, lat = float(raw_lon), float(raw_lat)
    return LandingCentre(
        name=name,
        district=_clean_str(props.get("DIST_NAME")),
        state=_clean_str(props.get("SECTOR_NAM")),
        lat=lat,
        lon=lon,
    )


class IncoisWFS(Source):
    """WFS access to the INCOIS PFZ, ecological-sensitivity, HAB and bathymetry
    GeoServer workspaces. Everything is keyless; everything needs the browser UA +
    Referer that ``Source.get`` supplies automatically."""

    source_id = "incois_wfs"
    source_name = "INCOIS GeoServer (WFS)"
    authority = "INCOIS"
    validity = timedelta(days=1)
    cache_ttl_s = 1800.0
    spatial_resolution_m = None  # every layer here is vector features

    # -- generic transport ---------------------------------------------------------

    def wfs(
        self,
        workspace: str,
        type_name: str,
        *,
        bbox: Sequence[float] | None = None,
        cql: str | None = None,
        max_features: int | None = None,
        srs: str = "EPSG:4326",
        retries: int = 2,
    ) -> FetchResult:
        """Generic INCOIS WFS 1.0.0 ``GetFeature`` -> GeoJSON ``FetchResult``.

        Tries the workspace-specific ``<workspace>/ows`` path first (confirmed live for
        all six workspaces used here). On a 404 only, falls back to the generic
        ``geoserver/ows`` with the same fully-qualified ``type_name`` — per the brief,
        though a direct probe of that bare path returned a 403, so treat it as
        best-effort, not a guaranteed rescue.
        """
        params: dict[str, Any] = {
            "service": "WFS",
            "version": "1.0.0",
            "request": "GetFeature",
            "typeName": type_name,
            "outputFormat": "application/json",
            "srsName": srs,
        }
        if bbox is not None:
            params["BBOX"] = ",".join(str(v) for v in bbox)
        if cql:
            params["CQL_FILTER"] = cql
        if max_features is not None:
            params["maxFeatures"] = max_features

        url = f"{INCOIS_GEOSERVER}/{workspace}/ows"
        try:
            return self.get(url, params=params, as_json=True, retries=retries)
        except SourceError as exc:
            if exc.status == 404:
                fallback_url = f"{INCOIS_GEOSERVER}/ows"
                return self.get(fallback_url, params=params, as_json=True, retries=retries)
            raise

    # -- named fetchers --------------------------------------------------------------

    def pfz_lines(self, bbox: Sequence[float] | None = None) -> tuple[list[dict], FetchResult]:
        bbox = bbox if bbox is not None else self.region.bbox
        raw = self.wfs(*_LAYERS["pfz_lines"], bbox=bbox, max_features=200)
        feats = bbox_filter(_features(raw), bbox)
        return feats, raw

    def pfz_sectors(self, sector_id: str | None = None) -> tuple[list[dict], FetchResult]:
        sector_id = sector_id or self.region.source("incois_pfz_sector")
        cql = f"SEC_ID='{sector_id}'" if sector_id else None
        raw = self.wfs(*_LAYERS["pfz_sectors"], cql=cql, max_features=50)
        return _features(raw), raw

    def landing_centres(self, bbox: Sequence[float] | None = None) -> tuple[list[dict], FetchResult]:
        bbox = bbox if bbox is not None else self.region.bbox
        raw = self.wfs(*_LAYERS["landing_centres"], bbox=bbox, max_features=2000)
        feats = bbox_filter(_features(raw), bbox)
        return feats, raw

    def eco_zones(self, kind: str) -> tuple[list[dict], FetchResult]:
        if kind not in _ECO_TYPE_NAMES:
            raise ValueError(f"unknown eco zone kind {kind!r}; expected one of {sorted(_ECO_TYPE_NAMES)}")
        bbox = self.region.bbox
        raw = self.wfs("MHW", _ECO_TYPE_NAMES[kind], bbox=bbox, max_features=50)
        feats = bbox_filter(_features(raw), bbox)
        return feats, raw

    def hab_sectors(self) -> tuple[list[dict], FetchResult]:
        raw = self.wfs(*_LAYERS["hab_sectors"], max_features=50)
        # No server-side bbox for this layer (only 4 features nationally) — filter locally.
        feats = bbox_filter(_features(raw), self.region.bbox)
        return feats, raw

    def bathymetry(self, bbox: Sequence[float] | None = None) -> tuple[list[dict], FetchResult]:
        bbox = bbox if bbox is not None else self.region.bbox
        raw = self.wfs(*_LAYERS["bathymetry"], bbox=bbox, max_features=5000)
        feats = bbox_filter(_features(raw), bbox)
        return feats, raw

    # -- typed outputs -----------------------------------------------------------------

    def nearest_pfz_line(self, lat: float, lon: float) -> tuple[Observation, dict[str, Any]] | None:
        feats, raw = self.pfz_lines(bbox=self.region.bbox)
        if not feats:
            # INCOIS does not issue PFZ lines every day (e.g. during the fishing ban) —
            # a valid outcome, not an error.
            return None

        best_feat: dict[str, Any] | None = None
        best_dist_nm = math.inf
        best_point: tuple[float, float] | None = None
        for feat in feats:
            for vlon, vlat in _iter_vertices(feat.get("geometry")):
                d = haversine_nm(lat, lon, vlat, vlon)
                if d < best_dist_nm:
                    best_dist_nm = d
                    best_feat = feat
                    best_point = (vlat, vlon)

        if best_feat is None or best_point is None:
            return None

        props = best_feat.get("properties") or {}
        year, julian_day = props.get("Year"), props.get("Julian_day")
        advisory_date = _advisory_date(year, julian_day)
        bearing = bearing_deg(lat, lon, best_point[0], best_point[1])

        prov = self.provenance(
            raw,
            issued_at=advisory_date,
            spatial_resolution_m=None,
            is_derived=False,
            notes=(
                "Official INCOIS Potential Fishing Zone (PFZ) advisory line "
                "(PFZ_Automation:pfzlines) — not a FORESHORE-derived product."
            ),
        )
        obs = self.observe(
            "nearest_pfz_line_distance",
            round(best_dist_nm, 3),
            "nm",
            lat,
            lon,
            valid_time=advisory_date or raw.acquired_at,
            provenance=prov,
        )
        payload = {
            "bearing_deg": round(bearing, 1),
            "advisory_year": year,
            "julian_day": julian_day,
            "advisory_date": advisory_date.isoformat() if advisory_date else None,
            "geometry": best_feat.get("geometry"),
            "closest_point": [best_point[0], best_point[1]],
        }
        return obs, payload

    def nearest_landing_centres(self, lat: float, lon: float, n: int = 3) -> list[LandingCentre]:
        feats, _raw = self.landing_centres(bbox=self.region.bbox)
        centres = [c for c in (_landing_centre_from_feature(f) for f in feats) if c is not None]
        if not centres:
            # Vessel near the region edge with nothing inside the configured bbox — this
            # feeds a DO_NOT_ADVISE handoff, so widen nationally rather than come back
            # empty.
            feats, _raw = self.landing_centres(bbox=(-180.0, -90.0, 180.0, 90.0))
            centres = [c for c in (_landing_centre_from_feature(f) for f in feats) if c is not None]

        located = [
            LandingCentre(
                name=c.name, district=c.district, state=c.state, lat=c.lat, lon=c.lon,
                distance_nm=haversine_nm(lat, lon, c.lat, c.lon),
            )
            for c in centres
        ]
        located.sort(key=lambda c: c.distance_nm if c.distance_nm is not None else math.inf)
        return located[:n]

    def parse_hab(self) -> list[Observation]:
        feats, raw = self.hab_sectors()
        lat, lon = self.region.centre
        prov = self.provenance(
            raw,
            valid_from=raw.acquired_at,
            is_derived=False,
            notes=(
                "INCOIS ABIS harmful algal bloom (HAB) sector membership "
                "(ABIS:HABSectors). This layer publishes sector boundaries only — it "
                "carries no live bloom-active/severity attribute."
            ),
        )
        observations: list[Observation] = []
        for feat in feats:
            props = feat.get("properties") or {}
            location = _clean_str(props.get("Location")) or f"HAB sector {props.get('Id')}"
            observations.append(
                self.observe(
                    "hab_sector_status",
                    location,
                    "category",
                    lat,
                    lon,
                    valid_time=raw.acquired_at,
                    provenance=prov,
                    sector_id=props.get("Id"),
                    geometry=feat.get("geometry"),
                    note="no live bloom-active field is published by ABIS:HABSectors; this is sector membership only",
                )
            )
        return observations

    # -- generic Source contract --------------------------------------------------------

    def parse(self, raw: FetchResult, **kw: Any) -> list[Observation]:
        """Best-effort generic parse of an arbitrary WFS ``FetchResult`` from this
        adapter, dispatching on the ``typeName`` embedded in the request URL. Used when
        a caller only has a raw :class:`FetchResult` (e.g. from :meth:`fetch`) rather
        than having called one of the named, typed fetchers above."""
        feats = _features(raw)
        type_name = (_type_name_of(raw.url) or "").lower()
        lat, lon = self.region.centre

        if "pfzlines" in type_name:
            prov = self.provenance(raw, is_derived=False, notes="Official INCOIS PFZ advisory line.")
            out = []
            for feat in feats:
                props = feat.get("properties") or {}
                uid = _clean_str(props.get("UID")) or _clean_str(props.get("Sno"))
                if uid is None:
                    continue
                out.append(
                    self.observe(
                        "pfz_line_id", uid, "id", lat, lon,
                        valid_time=raw.acquired_at, provenance=prov,
                        year=props.get("Year"), julian_day=props.get("Julian_day"),
                        length=props.get("Length"), geometry=feat.get("geometry"),
                    )
                )
            return out

        if "sector_new" in type_name:
            prov = self.provenance(raw, valid_from=raw.acquired_at, is_derived=False)
            return [
                self.observe(
                    "pfz_sector_id", sec_id, "id", lat, lon,
                    valid_time=raw.acquired_at, provenance=prov,
                    sector_name=(feat.get("properties") or {}).get("SECTORNAME"),
                )
                for feat in feats
                if (sec_id := _clean_str((feat.get("properties") or {}).get("SEC_ID"))) is not None
            ]

        if "landingcenters" in type_name:
            prov = self.provenance(raw, valid_from=raw.acquired_at, is_derived=False)
            out = []
            for feat in feats:
                centre = _landing_centre_from_feature(feat)
                if centre is None:
                    continue
                out.append(
                    self.observe(
                        "landing_centre_name", centre.name, "name", centre.lat, centre.lon,
                        valid_time=raw.acquired_at, provenance=prov,
                        district=centre.district, state=centre.state,
                    )
                )
            return out

        if "habsectors" in type_name:
            return self.parse_hab()

        if "bathymetry" in type_name:
            prov = self.provenance(raw, valid_from=raw.acquired_at, is_derived=False, notes="INCOIS bathymetry contour.")
            out = []
            for feat in feats:
                props = feat.get("properties") or {}
                depth = props.get("BATHMETRY1")
                if depth is None:
                    continue
                out.append(
                    self.observe(
                        "bathymetry_contour_depth", float(depth), "m", lat, lon,
                        valid_time=raw.acquired_at, provenance=prov,
                        label=props.get("BATHYLABEL"), geometry=feat.get("geometry"),
                    )
                )
            return out

        for kind, tname in _ECO_TYPE_NAMES.items():
            if tname.lower() in type_name:
                prov = self.provenance(raw, valid_from=raw.acquired_at, is_derived=False,
                                        notes=f"INCOIS MHW ecologically sensitive zone ({kind}).")
                return [
                    self.observe(
                        "eco_sensitive_zone_present", kind, "category", lat, lon,
                        valid_time=raw.acquired_at, provenance=prov,
                        geometry=feat.get("geometry"),
                    )
                    for feat in feats
                ]

        return []

    def fetch(self, **kwargs: Any) -> FetchResult:
        """Defaults to the official PFZ advisory lines layer."""
        bbox = kwargs.get("bbox", self.region.bbox)
        _feats, raw = self.pfz_lines(bbox=bbox)
        return raw

    def health(self) -> dict[str, Any]:
        """Probe every layer independently and count each. An empty result (e.g. no
        PFZ lines issued today) is a valid outcome; a transport failure on a layer is
        recorded in ``error`` and that layer's count is ``None`` — never hidden and
        never faked into a 0."""
        t0 = time.perf_counter()
        layers: dict[str, int | None] = {}
        errors: dict[str, str] = {}
        pfz_raw: FetchResult | None = None

        def probe(name: str, fn: Any) -> None:
            nonlocal pfz_raw
            try:
                feats, raw = fn()
                layers[name] = len(feats)
                if name == "pfz_lines":
                    pfz_raw = raw
            except Exception as exc:  # noqa: BLE001 - a layer failing must not kill the others
                layers[name] = None
                errors[name] = f"{type(exc).__name__}: {exc}"

        probe("pfz_lines", lambda: self.pfz_lines())
        probe("pfz_sectors", lambda: self.pfz_sectors())
        probe("landing_centres", lambda: self.landing_centres())
        probe("eco_coral", lambda: self.eco_zones("coral"))
        probe("eco_seagrass", lambda: self.eco_zones("seagrass"))
        probe("eco_mangrove", lambda: self.eco_zones("mangrove"))
        probe("hab_sectors", lambda: self.hab_sectors())
        probe("bathymetry", lambda: self.bathymetry())

        latency_ms = int((time.perf_counter() - t0) * 1000)
        counted = [v for v in layers.values() if isinstance(v, int)]
        return {
            "source_id": self.source_id,
            "ok": not errors,
            "count": sum(counted),
            "latency_ms": latency_ms,
            "issued_at": pfz_raw.acquired_at.isoformat() if pfz_raw else None,
            "freshness": None,
            "resolution_m": self.spatial_resolution_m,
            "error": "; ".join(f"{k}: {v}" for k, v in errors.items()) or None,
            "layers": layers,
        }


__all__ = ["INCOIS_GEOSERVER", "LandingCentre", "IncoisWFS"]
