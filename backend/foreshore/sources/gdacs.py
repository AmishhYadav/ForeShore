"""GDACS (Global Disaster Alert and Coordination System) tropical cyclone adapter.

Two keyless JSON endpoints, both probed live on 2026-08-31 before writing any parser:

* Event list — ``GDACS_EVENTLIST?eventlist=TC`` returns a GeoJSON ``FeatureCollection``
  of ``Point`` features (event *centroid*), one per tracked tropical-cyclone event
  worldwide. Real property names found on the wire: ``eventtype, eventid, episodeid,
  eventname, glide, name, description, htmldescription, icon, iconoverall, url
  {geometry, report, details}, alertlevel, alertscore, episodealertlevel,
  episodealertscore, istemporary, iscurrent, country, fromdate, todate, datemodified,
  iso3, source, sourceid, polygonlabel, Class, countryonland, affectedcountries,
  severitydata {severity, severitytext, severityunit}``. ``iscurrent`` arrives as the
  *string* ``"true"``/``"false"``, not a JSON bool. Every feature also carries its own
  feature-level ``bbox`` (degenerate to the point itself here, since geometry is a
  Point) — used for the region-proximity check below. On the probe date the list held
  19 historical/tracked TC events worldwide and exactly **one** with ``iscurrent ==
  "true"`` (SAUDEL-26, off Japan) — GDACS defines "current" globally, not per-region, so
  zero current events near Palk Bay / Gulf of Mannar is the expected common case, not a
  failure.

* Geometry — ``GDACS_GEOMETRY?eventtype=TC&eventid=<id>&episodeid=<ep>`` returns a
  ``FeatureCollection`` mixing several feature kinds for one cyclone episode,
  distinguished by the ``Class`` property (not ``featuretype``, which is only present on
  some of them):

  - ``Poly_Cones``      — MultiPolygon, forecast-track uncertainty cone
  - ``Poly_Red`` / ``Poly_Orange`` / ``Poly_Green`` — MultiPolygon wind-radii bands per
    forecast step (``featuretype = "WindRadii"``), red = highest wind speed
  - ``Point_Centroid``  — the current/latest position (Point)
  - ``Point_Polygon_Point_N`` — per-forecast-step position markers (``featuretype =
    "PointRadii"``)
  - ``Line_Line_N``     — track LineStrings, one per storm-category segment (``forecast``
    True/False marks the forecast vs. observed portion)

  Every feature here also carries its own feature-level ``bbox``. Probed against a past
  episode (DITWAH-25, eventid 1001238, episodeid 12) since no globally-current event sat
  near this region on the probe date; the schema does not vary by episode.

0 active/nearby cyclones is a valid, common outcome — not an error. ``health()`` reports
``ok=True`` with a ``no_active_cyclone`` fact rather than failing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from ..models import UTC, Observation, Provenance, utcnow
from .base import FetchResult, Source

GDACS_EVENTLIST = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
GDACS_GEOMETRY = "https://www.gdacs.org/gdacsapi/api/polygons/getgeometry"

#: Degrees of padding added to the region bbox when deciding whether a cyclone
#: "matters" to this coast. ~5 degrees is ~550 km at this latitude — a storm that far
#: out can still raise swell and wind that reach a small boat long before its eye does.
REGION_BUFFER_DEG = 5.0

_WIND_CLASSES = {"Poly_Red": "wind_red", "Poly_Orange": "wind_orange", "Poly_Green": "wind_green"}


def _parse_dt(value: Any) -> datetime | None:
    """GDACS timestamps arrive as naive ISO strings ("2026-08-18T12:00:00"), UTC."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = s[:-1] if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def _feature_point(feat: dict[str, Any]) -> tuple[float | None, float | None]:
    geom = feat.get("geometry") or {}
    if geom.get("type") != "Point":
        return None, None
    coords = geom.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None, None
    lon, lat = float(coords[0]), float(coords[1])
    return lat, lon


@dataclass(frozen=True)
class CycloneEvent:
    event_id: str
    episode_id: str
    name: str
    alert_level: str
    from_date: datetime | None
    to_date: datetime | None
    lat: float | None
    lon: float | None
    country: str | None
    severity: str | None
    is_current: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "episode_id": self.episode_id,
            "name": self.name,
            "alert_level": self.alert_level,
            "from_date": self.from_date.isoformat() if self.from_date else None,
            "to_date": self.to_date.isoformat() if self.to_date else None,
            "lat": self.lat,
            "lon": self.lon,
            "country": self.country,
            "severity": self.severity,
            "is_current": self.is_current,
        }


def _event_from_feature(feat: dict[str, Any]) -> CycloneEvent | None:
    props = feat.get("properties") or {}
    event_id = props.get("eventid")
    episode_id = props.get("episodeid")
    if event_id is None or episode_id is None:
        return None
    lat, lon = _feature_point(feat)
    severity = None
    sev = props.get("severitydata")
    if isinstance(sev, dict):
        severity = sev.get("severitytext") or (
            f"{sev.get('severity')} {sev.get('severityunit')}".strip()
            if sev.get("severity") is not None
            else None
        )
    return CycloneEvent(
        event_id=str(event_id),
        episode_id=str(episode_id),
        name=str(props.get("eventname") or props.get("name") or f"TC-{event_id}"),
        alert_level=str(props.get("alertlevel") or "Green"),
        from_date=_parse_dt(props.get("fromdate")),
        to_date=_parse_dt(props.get("todate")),
        lat=lat,
        lon=lon,
        country=props.get("country"),
        severity=severity,
        is_current=_as_bool(props.get("iscurrent")),
    )


def _events_from_raw(raw: FetchResult) -> list[CycloneEvent]:
    payload = raw.payload
    feats = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(feats, list):
        return []
    out = []
    for feat in feats:
        ev = _event_from_feature(feat)
        if ev is not None:
            out.append(ev)
    return out


def _buffered_bbox(bbox: Sequence[float], pad_deg: float) -> tuple[float, float, float, float]:
    minlon, minlat, maxlon, maxlat = bbox
    return (minlon - pad_deg, minlat - pad_deg, maxlon + pad_deg, maxlat + pad_deg)


def _point_in_bbox(lat: float | None, lon: float | None, bbox: Sequence[float]) -> bool:
    if lat is None or lon is None:
        return False
    minlon, minlat, maxlon, maxlat = bbox
    return minlat <= lat <= maxlat and minlon <= lon <= maxlon


def _feature_bbox_overlaps(feat: dict[str, Any], bbox: Sequence[float]) -> bool:
    fb = feat.get("bbox")
    if not (isinstance(fb, (list, tuple)) and len(fb) == 4):
        return False
    fminlon, fminlat, fmaxlon, fmaxlat = fb
    minlon, minlat, maxlon, maxlat = bbox
    return fmaxlon >= minlon and fminlon <= maxlon and fmaxlat >= minlat and fminlat <= maxlat


class GDACSCyclones(Source):
    """GDACS tropical-cyclone event list + per-episode hazard geometry. Keyless."""

    source_id = "gdacs_tc"
    source_name = "GDACS Tropical Cyclone alerts (JRC / European Commission)"
    authority = "JRC/GDACS"
    validity = timedelta(hours=6)
    cache_ttl_s = 900.0
    spatial_resolution_m = None  # vector hazard polygons, not a raster product

    # -- fetchers ------------------------------------------------------------------

    def _fetch_eventlist(self) -> FetchResult:
        return self.get(GDACS_EVENTLIST, params={"eventlist": "TC"}, as_json=True)

    def events(self, *, current_only: bool = True) -> tuple[list[CycloneEvent], FetchResult]:
        raw = self._fetch_eventlist()
        evs = _events_from_raw(raw)
        if current_only:
            evs = [e for e in evs if e.is_current]
        return evs, raw

    def events_near_region(self) -> list[CycloneEvent]:
        """Current events whose centroid falls inside the region bbox buffered by
        ``REGION_BUFFER_DEG``. 0 is a valid, common outcome — GDACS "current" is a
        global count and most days have no cyclone anywhere near this coast."""
        evs, _raw = self.events(current_only=True)
        bbox = _buffered_bbox(self.region.bbox, REGION_BUFFER_DEG)
        return [e for e in evs if _point_in_bbox(e.lat, e.lon, bbox)]

    def geometry(self, event_id: str, episode_id: str) -> tuple[dict[str, Any], FetchResult]:
        """Fetch and classify one episode's hazard geometry by its ``Class`` property."""
        raw = self.get(
            GDACS_GEOMETRY,
            params={"eventtype": "TC", "eventid": event_id, "episodeid": episode_id},
            as_json=True,
        )
        out: dict[str, list[dict[str, Any]]] = {
            "cones": [], "wind_red": [], "wind_orange": [], "wind_green": [],
            "track": [], "points": [],
        }
        payload = raw.payload
        feats = payload.get("features") if isinstance(payload, dict) else None
        for feat in feats or []:
            cls = str((feat.get("properties") or {}).get("Class") or "")
            if cls == "Poly_Cones":
                out["cones"].append(feat)
            elif cls in _WIND_CLASSES:
                out[_WIND_CLASSES[cls]].append(feat)
            elif cls.startswith("Line_Line"):
                out["track"].append(feat)
            elif cls.startswith("Point_"):
                out["points"].append(feat)
        return out, raw

    def exclusion_polygons(self) -> tuple[list[dict[str, Any]], list[Observation]]:
        """Cone + red/orange wind polygons for every nearby current event, as tagged
        GeoJSON features, plus one categorical Observation per event."""
        events = self.events_near_region()
        polygons: list[dict[str, Any]] = []
        observations: list[Observation] = []

        for ev in events:
            geo, raw = self.geometry(ev.event_id, ev.episode_id)
            prov = self.provenance(
                raw,
                issued_at=ev.from_date,
                valid_from=ev.from_date,
                notes=(
                    f"GDACS tropical cyclone hazard geometry for {ev.name} "
                    f"(event {ev.event_id}/{ev.episode_id})."
                ),
            )
            for hazard_class, key in (
                ("cyclone_cone", "cones"),
                ("cyclone_wind_red", "wind_red"),
                ("cyclone_wind_orange", "wind_orange"),
            ):
                for feat in geo.get(key, []):
                    tagged = dict(feat)
                    props = dict(feat.get("properties") or {})
                    props.update({
                        "hazard_class": hazard_class,
                        "event_name": ev.name,
                        "alert_level": ev.alert_level,
                    })
                    tagged["properties"] = props
                    polygons.append(tagged)

            observations.append(
                self.observe(
                    "cyclone_alert_level",
                    ev.alert_level,
                    "category",
                    ev.lat if ev.lat is not None else self.region.centre[0],
                    ev.lon if ev.lon is not None else self.region.centre[1],
                    valid_time=raw.acquired_at,
                    provenance=prov,
                    event_id=ev.event_id,
                    episode_id=ev.episode_id,
                    event_name=ev.name,
                    country=ev.country,
                    severity=ev.severity,
                    is_current=ev.is_current,
                )
            )

        return polygons, observations

    # -- generic Source contract ----------------------------------------------------

    def parse(self, raw: FetchResult, **kw: Any) -> list[Observation]:
        """Turn an event-list ``FetchResult`` into one categorical Observation per
        event. ``current_only`` (default False) restricts to globally-current events."""
        current_only = bool(kw.get("current_only", False))
        evs = _events_from_raw(raw)
        if current_only:
            evs = [e for e in evs if e.is_current]
        prov = self.provenance(
            raw,
            valid_from=raw.acquired_at,
            notes="GDACS tropical cyclone event list (JRC/European Commission).",
        )
        lat0, lon0 = self.region.centre
        return [
            self.observe(
                "cyclone_alert_level",
                ev.alert_level,
                "category",
                ev.lat if ev.lat is not None else lat0,
                ev.lon if ev.lon is not None else lon0,
                valid_time=raw.acquired_at,
                provenance=prov,
                event_id=ev.event_id,
                episode_id=ev.episode_id,
                event_name=ev.name,
                country=ev.country,
                severity=ev.severity,
                is_current=ev.is_current,
            )
            for ev in evs
        ]

    def fetch(self, **kwargs: Any) -> FetchResult:
        return self._fetch_eventlist()

    def health(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            current_evs, raw = self.events(current_only=True)
            near = self.events_near_region()
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {
                "source_id": self.source_id,
                "ok": True,
                "count": len(near),
                "latency_ms": latency_ms,
                "issued_at": raw.acquired_at.isoformat(),
                "freshness": None,
                "resolution_m": self.spatial_resolution_m,
                "error": None,
                "events_current_global": len(current_evs),
                "events_near_region": len(near),
                "no_active_cyclone": len(current_evs) == 0,
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
                "events_current_global": None,
                "events_near_region": None,
                "no_active_cyclone": None,
            }


__all__ = ["GDACS_EVENTLIST", "GDACS_GEOMETRY", "CycloneEvent", "GDACSCyclones"]
