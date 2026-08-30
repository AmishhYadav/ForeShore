"""IMD GeoServer adapter — reactjs.imd.gov.in/geoserver/imd/wfs.

WFS 1.0.0, ``outputFormat=application/json``. 403s without a browser ``User-Agent`` and
a ``Referer``; both are added by :meth:`Source.get`, so every request in this module
goes through ``self.get`` / ``self.wfs`` and never touches ``httpx`` directly.

Schemas below were pulled live on 2026-08-30 with ``maxFeatures=2`` probes plus
``DescribeFeatureType`` (which returns the XSD regardless of feature count — the only
way to see ``Cyclone_Track_V``'s schema while no cyclone is active). Recorded here so
nobody has to re-probe.

**Operational surprise, verified by direct test, contradicting the brief this module was
built from**: this GeoServer instance rejects ``BBOX`` and ``CQL_FILTER`` together —
``"bbox and cql_filter both specified but are mutually exclusive"`` — even when the CQL
is a plain attribute predicate with no spatial component. :meth:`IMDGeoServer.wfs`
enforces "exactly one, never both" and every named fetcher below picks a single filter
accordingly (BBOX when scoping to the active region; CQL_FILTER, unscoped, for a named
lookup like one district).

``imd:NowcastWarningDistrict`` (district-level nowcast/lightning warnings; ~1.3 MB / 764
districts unfiltered nationwide — always constrain with BBOX or CQL_FILTER):
    ``id`` (int), ``Fid`` (str), ``Obj_id`` (int), ``Date`` ("YYYY-MM-DD" str),
    ``State_District`` (str, state+district concatenated with no separator),
    ``cat1``..``cat19`` (int; ``cat16`` is typed as string in the XSD and is usually
    ``""``) — **no legend for these is published on this endpoint**; on every feature
    probed here (calm weather, all of Tamil Nadu, 2026-08-30) ``cat1=1`` and every other
    cat is ``0``/``""`` regardless of district, so they read as an opaque "no active
    category" baseline rather than a decodable signal. Carried through as raw
    qualifiers, never used to derive the observation value.
    ``message`` / ``impact`` / ``action`` (str, free text — the fields to surface when
    IMD actually posts a warning; **empty on every feature observed** in this probe).
    ``toi`` (str, "HHMM", time of issue — no timezone in the schema; IMD's own
    convention is IST, followed here as a documented assumption, not a confirmed fact).
    ``vupto`` (str, "HHMM", valid-until — same caveat; can roll past midnight relative
    to ``toi``, e.g. ``toi="2200"``, ``vupto="0100"``).
    ``Color`` (int; every calm-weather feature seen here is ``1`` — inferred, *not
    confirmed*, to follow IMD's public green/yellow/orange/red 4-level code).
    ``update_time`` (ISO-8601 UTC ``dateTime``, e.g. ``"2026-08-30T21:55:12Z"`` —
    unambiguous, used as the issued_at fallback).
    ``MC_RMC`` (str, issuing met/regional centre, e.g. ``"mc_mizoram"``).
    ``District`` (str, upper-case). ``State`` (str, upper-case, **no space** —
    ``"TAMILNADU"``, not ``"TAMIL NADU"``; a ``CQL_FILTER`` on ``State`` must match
    that). ``Data`` (str, empty on every feature seen).

``imd:aws_data_layer`` (~2071 AWS/ARG stations nationwide; always BBOX-scope to the
region — a handful of stations, not 2000):
    ``geom`` (Point), ``id`` (str, station code), ``call_sign`` (str),
    ``station_id`` (int, frequently null), ``dat`` (date), ``time`` (time — often a
    stale placeholder such as ``1970-01-01T01:00:00Z``, ignore it), ``rain_sel`` (str),
    ``rainfall`` (str, mm), ``temp``/``temp_min``/``temp_max``/``temp_min_max`` (str,
    deg C — typed as *string* in the XSD; ``"NULL"`` is the sentinel for missing, not
    JSON null), ``dewpoint`` (decimal, deg C), ``weather`` (int code, undocumented),
    ``nebulosity`` (int code, undocumented), ``rh`` (str, %), ``winddir`` (str, deg),
    ``windspeed`` (str — **unit not published by this API**; followed here as km/h
    per IMD's public AWS/ARG portal convention, a documented assumption, not a
    discovered fact), ``mslp`` (str, hPa), ``winddir2``/``windspeed2`` (str, secondary
    sensor, usually empty), ``update_time`` (str "YYYY-MM-DD HH:MM:SS", no offset —
    treated as UTC, consistent with the surrounding FeatureCollection's own UTC
    ``timeStamp``), ``station`` (str, station name, upper-case, sometimes padded).
    ``"NULL"`` (the literal string) is this layer's null sentinel throughout; filtered
    out before any numeric coercion.

``imd:Cyclone_Track_V`` (0 features at probe time — **no active cyclone, a valid
    state, not an error** — so the live GetFeature response could not be used to
    discover this schema; pulled instead from ``DescribeFeatureType``, which returns
    the XSD independent of feature count):
    ``id`` (long), ``geom`` (geometry), ``cyclone_id`` (int), ``cyclone_type`` (str).
    That is the *complete* attribute list — no storm name, category or per-point
    timestamp field exists on this layer at all. :meth:`IMDGeoServer.parse_cyclone`
    reports whatever ``cyclone_id``/``cyclone_type`` it gets and falls back to fetch
    time for ``valid_time`` because the schema carries no timestamp to parse.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import UTC, Authority, Observation, haversine_m
from .base import FetchResult, Source, SourceError

log = logging.getLogger("foreshore.sources.imd_geoserver")

IMD_WFS = "https://reactjs.imd.gov.in/geoserver/imd/wfs"

NOWCAST_LAYER = "imd:NowcastWarningDistrict"
AWS_LAYER = "imd:aws_data_layer"
CYCLONE_LAYER = "imd:Cyclone_Track_V"

#: toi/vupto carry no timezone in the schema; IMD's own convention is IST. Documented
#: assumption, not a confirmed fact (see module docstring).
_IST = timezone(timedelta(hours=5, minutes=30))

#: Inferred (not confirmed) mapping of the nowcast `Color` code to IMD's public
#: green/yellow/orange/red 4-level warning code.
_COLOR_LABEL: dict[int, str] = {1: "green", 2: "yellow", 3: "orange", 4: "red"}

#: AWS field -> (Observation.variable, unit). Only variables actually present and
#: numeric on a given station are emitted.
_AWS_VARS: dict[str, tuple[str, str]] = {
    "temp": ("temperature", "degC"),
    "windspeed": ("wind_speed", "km/h"),
    "winddir": ("wind_direction", "deg"),
    "rh": ("humidity", "%"),
    "mslp": ("pressure", "hPa"),
    "rainfall": ("rainfall", "mm"),
}

_LAYER_ALIASES: dict[str, str] = {
    "nowcast": NOWCAST_LAYER,
    "nowcast_warning": NOWCAST_LAYER,
    "aws": AWS_LAYER,
    "cyclone": CYCLONE_LAYER,
    "cyclone_track": CYCLONE_LAYER,
}


# ------------------------------------------------------------------------------------
# small stateless helpers — geometry + value parsing
# ------------------------------------------------------------------------------------


def _flatten_coords(node: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    if node is None:
        return out
    if isinstance(node, (list, tuple)):
        if node and isinstance(node[0], (int, float)) and len(node) >= 2:
            out.append((float(node[0]), float(node[1])))
            return out
        for child in node:
            out.extend(_flatten_coords(child))
    return out


def _centroid(geometry: dict[str, Any] | None) -> tuple[float | None, float | None]:
    """Cheap vertex-average centroid — good enough for pinning a district polygon."""
    if not geometry:
        return None, None
    coords = _flatten_coords(geometry.get("coordinates"))
    if not coords:
        return None, None
    lat = sum(c[1] for c in coords) / len(coords)
    lon = sum(c[0] for c in coords) / len(coords)
    return lat, lon


def _point(geometry: dict[str, Any] | None) -> tuple[float | None, float | None]:
    if not geometry:
        return None, None
    if geometry.get("type") == "Point":
        coords = geometry.get("coordinates") or []
        if len(coords) < 2:
            return None, None
        return float(coords[1]), float(coords[0])
    return _centroid(geometry)  # defensive fallback for an unexpected geometry type


def _numeric(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.upper() == "NULL":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_iso(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_hhmm_on_date(date_str: Any, hhmm: Any) -> datetime | None:
    """Combine nowcast's ``Date`` ("YYYY-MM-DD") + ``toi``/``vupto`` ("HHMM") into a
    UTC datetime, assuming IST (see module docstring caveat)."""
    if not date_str or not hhmm:
        return None
    digits = str(hhmm).strip()
    if not digits.isdigit():
        return None
    digits = digits.zfill(4)
    try:
        hh, mm = int(digits[:2]) % 24, int(digits[2:4]) % 60
        d = datetime.strptime(str(date_str).strip(), "%Y-%m-%d")
        local = d.replace(hour=hh, minute=mm, tzinfo=_IST)
        return local.astimezone(UTC)
    except ValueError:
        return None


def _parse_aws_update_time(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _nowcast_value(p: dict[str, Any]) -> str:
    for key in ("message", "impact", "action"):
        v = p.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "NIL"


def _sniff_layer(url: str) -> str | None:
    for layer in (NOWCAST_LAYER, AWS_LAYER, CYCLONE_LAYER):
        if layer.split(":", 1)[-1] in url:
            return layer
    return None


# ------------------------------------------------------------------------------------


class IMDGeoServer(Source):
    """IMD GeoServer adapter: district nowcasts, AWS observations, cyclone track."""

    source_id = "imd_geoserver"
    source_name = "IMD GeoServer (reactjs.imd.gov.in)"
    authority: Authority = "IMD"
    base_url = IMD_WFS
    validity = timedelta(hours=3)
    cache_ttl_s = 300.0

    # -- generic WFS -------------------------------------------------------------------

    def wfs(
        self,
        type_name: str,
        *,
        bbox: tuple[float, ...] | None = None,
        cql: str | None = None,
        max_features: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> FetchResult:
        """Generic WFS 1.0.0 GetFeature returning parsed GeoJSON in ``FetchResult.payload``.

        BBOX and CQL_FILTER are mutually exclusive on this server (verified live —
        see module docstring); pass exactly one, never both.
        """
        if bbox is not None and cql:
            raise ValueError(
                "IMD GeoServer rejects BBOX and CQL_FILTER together (verified live "
                "2026-08-30); pass exactly one"
            )
        params: dict[str, Any] = {
            "service": "WFS",
            "version": "1.0.0",
            "request": "GetFeature",
            "typeName": type_name,
            "outputFormat": "application/json",
        }
        if bbox is not None:
            params["BBOX"] = ",".join(str(v) for v in bbox)
        if cql:
            params["CQL_FILTER"] = cql
        if max_features is not None:
            params["maxFeatures"] = int(max_features)
        if extra:
            params.update(extra)
        raw = self.get(self.base_url, params=params, as_json=True)
        payload = raw.payload
        if not isinstance(payload, dict) or "features" not in payload:
            raise SourceError(
                self.source_id,
                f"unexpected WFS response for {type_name}: {str(payload)[:200]!r}",
            )
        return raw

    # -- named fetchers ------------------------------------------------------------

    def nowcast_warnings(self, district: str | None = None) -> tuple[list[dict], FetchResult]:
        if district:
            safe = str(district).strip().upper().replace("'", "''")
            raw = self.wfs(NOWCAST_LAYER, cql=f"strToUpperCase(District)='{safe}'")
        else:
            raw = self.wfs(NOWCAST_LAYER, bbox=self.region.bbox)
        return list(raw.payload.get("features", [])), raw

    def aws_observations(self) -> tuple[list[dict], FetchResult]:
        raw = self.wfs(AWS_LAYER, bbox=self.region.bbox)
        return list(raw.payload.get("features", [])), raw

    def cyclone_track(self) -> tuple[list[dict], FetchResult]:
        # Intentionally unscoped: a cyclone worth alerting on is often still outside
        # the small regional bbox while it approaches.
        raw = self.wfs(CYCLONE_LAYER)
        return list(raw.payload.get("features", [])), raw

    # -- typed outputs ---------------------------------------------------------------

    def parse_nowcast(self, district: str | None = None) -> list[Observation]:
        feats, raw = self.nowcast_warnings(district=district)
        return self._parse_nowcast_features(feats, raw, district=district)

    def parse_aws(
        self, lat: float | None = None, lon: float | None = None, max_results: int = 5
    ) -> list[Observation]:
        feats, raw = self.aws_observations()
        return self._parse_aws_features(feats, raw, lat=lat, lon=lon, max_results=max_results)

    def parse_cyclone(self) -> list[Observation]:
        feats, raw = self.cyclone_track()
        return self._parse_cyclone_features(feats, raw)

    def parse(self, raw: FetchResult, **kw: Any) -> list[Observation]:
        """Dispatches on ``layer`` (``NOWCAST_LAYER``/``AWS_LAYER``/``CYCLONE_LAYER``,
        or the short names ``"nowcast"``/``"aws"``/``"cyclone"``). Falls back to
        sniffing the type name out of ``raw.url`` when ``layer`` is omitted, so a
        ``FetchResult`` from :meth:`wfs` can be routed without the caller remembering
        which layer they asked for.
        """
        layer = kw.get("layer") or _sniff_layer(raw.url)
        layer = _LAYER_ALIASES.get(layer, layer)
        payload = raw.payload if isinstance(raw.payload, dict) else {}
        feats = list(payload.get("features", []))
        if layer == NOWCAST_LAYER:
            return self._parse_nowcast_features(feats, raw, district=kw.get("district"))
        if layer == AWS_LAYER:
            return self._parse_aws_features(
                feats, raw, lat=kw.get("lat"), lon=kw.get("lon"),
                max_results=kw.get("max_results", 5),
            )
        if layer == CYCLONE_LAYER:
            return self._parse_cyclone_features(feats, raw)
        raise SourceError(
            self.source_id,
            f"parse(): cannot determine layer from {raw.url!r}; pass layer=... explicitly",
        )

    def fetch(self, **kwargs: Any) -> FetchResult:
        """Defaults to the nowcast layer, BBOX-scoped to the active region."""
        district = kwargs.pop("district", None)
        if district:
            safe = str(district).strip().upper().replace("'", "''")
            return self.wfs(NOWCAST_LAYER, cql=f"strToUpperCase(District)='{safe}'", **kwargs)
        return self.wfs(NOWCAST_LAYER, bbox=self.region.bbox, **kwargs)

    # -- parsing internals -------------------------------------------------------------

    def _parse_nowcast_features(
        self, feats: list[dict], raw: FetchResult, *, district: str | None = None
    ) -> list[Observation]:
        target = district.strip().casefold() if district else None
        obs: list[Observation] = []
        for f in feats:
            p = f.get("properties") or {}
            if target is not None:
                d = str(p.get("District") or "").strip().casefold()
                if d != target:
                    continue
            lat, lon = _centroid(f.get("geometry"))
            if lat is None or lon is None:
                continue
            issued_at = (
                _parse_hhmm_on_date(p.get("Date"), p.get("toi"))
                or _parse_iso(p.get("update_time"))
                or raw.acquired_at
            )
            valid_to = _parse_hhmm_on_date(p.get("Date"), p.get("vupto"))
            if valid_to is not None and valid_to <= issued_at:
                valid_to += timedelta(days=1)  # vupto rolls past midnight relative to toi
            color = p.get("Color")
            prov = self.provenance(
                raw,
                issued_at=issued_at,
                valid_to=valid_to,
                notes=(
                    f"Color={color} read as '{_COLOR_LABEL.get(color, 'unknown')}' — "
                    "inferred from IMD's public 4-level code, not confirmed by this "
                    "endpoint's own schema; toi/vupto assumed IST"
                ),
            )
            obs.append(self.observe(
                variable="nowcast_warning",
                value=_nowcast_value(p),
                unit="category",
                lat=lat, lon=lon,
                valid_time=issued_at,
                provenance=prov,
                district=p.get("District"),
                state=p.get("State"),
                toi=p.get("toi"),
                vupto=p.get("vupto"),
                color=color,
                color_label=_COLOR_LABEL.get(color),
                message=p.get("message") or None,
                impact=p.get("impact") or None,
                action=p.get("action") or None,
                met_centre=p.get("MC_RMC"),
            ))
        return obs

    def _parse_aws_features(
        self,
        feats: list[dict],
        raw: FetchResult,
        *,
        lat: float | None = None,
        lon: float | None = None,
        max_results: int = 5,
    ) -> list[Observation]:
        if lat is None or lon is None:
            port = self.region.anchor_ports[0]
            lat, lon = port.lat, port.lon

        ranked: list[tuple[float, float, float, dict[str, Any]]] = []
        for f in feats:
            geom = f.get("geometry") or {}
            if geom.get("type") != "Point":
                continue
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                continue
            slon, slat = float(coords[0]), float(coords[1])
            dist_km = haversine_m(lat, lon, slat, slon) / 1000.0
            ranked.append((dist_km, slat, slon, f.get("properties") or {}))
        ranked.sort(key=lambda r: r[0])

        obs: list[Observation] = []
        for dist_km, slat, slon, p in ranked[: max(0, max_results)]:
            issued_at = _parse_aws_update_time(p.get("update_time")) or raw.acquired_at
            prov = self.provenance(
                raw,
                issued_at=issued_at,
                notes=(
                    "wind_speed unit (km/h) follows IMD's public AWS/ARG portal "
                    "convention — not confirmed by this API's own metadata"
                ),
            )
            station_name = str(p.get("station") or "").strip() or str(p.get("id") or "unknown")
            for field, (variable, unit) in _AWS_VARS.items():
                val = _numeric(p.get(field))
                if val is None:
                    continue
                obs.append(self.observe(
                    variable=variable,
                    value=val,
                    unit=unit,
                    lat=slat, lon=slon,
                    valid_time=issued_at,
                    provenance=prov,
                    station_name=station_name,
                    station_id=p.get("id"),
                    call_sign=p.get("call_sign"),
                    distance_km=round(dist_km, 2),
                ))
        return obs

    def _parse_cyclone_features(self, feats: list[dict], raw: FetchResult) -> list[Observation]:
        if not feats:
            log.info(
                "%s: %s returned 0 features -- no_active_cyclone (valid outcome, not an error)",
                self.source_id, CYCLONE_LAYER,
            )
            return []
        obs: list[Observation] = []
        for f in feats:
            p = f.get("properties") or {}
            lat, lon = _point(f.get("geometry"))
            if lat is None or lon is None:
                continue
            prov = self.provenance(
                raw,
                issued_at=raw.acquired_at,
                notes=(
                    "Cyclone_Track_V exposes only id/cyclone_id/cyclone_type (confirmed "
                    "via WFS DescribeFeatureType) — no per-point timestamp, storm name "
                    "or category field exists on this layer, so valid_time is fetch time"
                ),
            )
            obs.append(self.observe(
                variable="cyclone_track_point",
                value=str(p.get("cyclone_type") or "UNKNOWN"),
                unit="category",
                lat=lat, lon=lon,
                valid_time=raw.acquired_at,
                provenance=prov,
                cyclone_id=p.get("cyclone_id"),
                cyclone_type=p.get("cyclone_type"),
            ))
        return obs

    # -- health ----------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Probes all three layers; reports one summary row plus a per-layer count.

        ``ok=True`` when the cyclone layer is reachable but empty — that is the
        expected state absent an active storm, not a failure.
        """
        t0 = time.perf_counter()
        layers: dict[str, int] = {}
        errors: list[str] = []
        issued_at: datetime | None = None
        freshness: str | None = None
        ok = True
        count_total = 0

        try:
            nc = self.parse_nowcast()
            layers[NOWCAST_LAYER] = len(nc)
            count_total += len(nc)
            if nc:
                issued_at = nc[0].provenance.issued_at
                freshness = nc[0].provenance.freshness
        except Exception as exc:  # noqa: BLE001
            ok = False
            layers[NOWCAST_LAYER] = 0
            errors.append(f"{NOWCAST_LAYER}: {type(exc).__name__}: {exc}")

        try:
            aws = self.parse_aws()
            layers[AWS_LAYER] = len(aws)
            count_total += len(aws)
            if aws and issued_at is None:
                issued_at = aws[0].provenance.issued_at
                freshness = aws[0].provenance.freshness
        except Exception as exc:  # noqa: BLE001
            ok = False
            layers[AWS_LAYER] = 0
            errors.append(f"{AWS_LAYER}: {type(exc).__name__}: {exc}")

        try:
            cy = self.parse_cyclone()
            layers[CYCLONE_LAYER] = len(cy)  # 0 here is a valid, healthy outcome
            count_total += len(cy)
        except Exception as exc:  # noqa: BLE001
            ok = False
            layers[CYCLONE_LAYER] = 0
            errors.append(f"{CYCLONE_LAYER}: {type(exc).__name__}: {exc}")

        return {
            "source_id": self.source_id,
            "ok": ok,
            "count": count_total,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "issued_at": issued_at.isoformat() if issued_at else None,
            "freshness": freshness,
            "resolution_m": self.spatial_resolution_m,
            "error": "; ".join(errors) or None,
            "layers": layers,
        }


__all__ = ["IMDGeoServer", "IMD_WFS", "NOWCAST_LAYER", "AWS_LAYER", "CYCLONE_LAYER"]
