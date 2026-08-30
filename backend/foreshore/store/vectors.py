"""Vector feature store — geofences, landing centres, PFZ lines, IMBL segments, and any
other point/line/polygon layer FORESHORE reasons over.

Two backends behind one interface:

- **PostGIS** — used only when ``FORESHORE_PG_DSN`` is set, ``psycopg`` imports, and the
  server actually answers a trivial query at construction time. An accelerator, never a
  dependency.
- **File backend** — the default and the demo-safe path. Each layer lives as
  ``data/static/<layer_id>.geojson`` with a ``data/static/<layer_id>.meta.json`` sidecar,
  indexed in memory with :class:`shapely.strtree.STRtree`. This backend is complete and
  correct on its own; the geofence engine and the demo must never depend on Docker being
  up.

Geometry rules the geofence engine relies on:

- Reported distances are great-circle nautical miles from :func:`~foreshore.models.haversine_nm`,
  measured on the true closest point (:func:`shapely.ops.nearest_points`), never on a
  degree-space approximation. Degree-space distance is used only to narrow the STRtree
  candidate set before the real measurement.
- ``inside`` (and ``contains()``) are true only for polygonal geometries that actually
  contain the point — a point "inside" a LineString is not a meaningful geofence concept
  here.
- ``bearing_deg`` runs from the query point to the closest point, and is ``None`` when the
  distance is zero (the query point is inside the geometry).
- The in-memory STRtree is rebuilt whenever the backing GeoJSON file's mtime changes, so a
  file dropped in by an ingestion job is picked up without restarting anything.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Any

import orjson
from shapely.geometry import Point, box as shapely_box, shape as shapely_shape
from shapely.ops import nearest_points
from shapely.strtree import STRtree

from ..config import STATIC_DIR, env
from ..models import UTC, bearing_deg as _bearing_deg, haversine_nm

_POLYGONAL = {"Polygon", "MultiPolygon"}


def _ensure_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _ensure_aware(value)
    return _ensure_aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _feature_key(feature: dict[str, Any], key_property: str | None, index: int) -> str:
    """Stable key for upsert idempotency.

    Prefers ``properties[key_property]`` when present; otherwise falls back to a
    deterministic hash of the feature's geometry + properties so that writing the same
    feature twice (with no key column available) still upserts rather than duplicates.
    """
    props = feature.get("properties") or {}
    if key_property:
        val = props.get(key_property)
        if val not in (None, ""):
            return str(val)
    blob = json.dumps(
        {"g": feature.get("geometry"), "p": props}, sort_keys=True, default=str
    )
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _probe_postgis(dsn: str) -> Any | None:
    """Try to open a live PostGIS connection. Any failure (missing driver, unreachable
    server, bad DSN) degrades silently to ``None`` — the caller falls back to the file
    backend rather than raising."""
    try:
        import psycopg  # type: ignore
    except ImportError:
        return None
    try:
        conn = psycopg.connect(dsn, connect_timeout=2, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return conn
    except Exception:
        return None


# --------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Feature:
    """One stored vector feature. ``geometry`` is a raw GeoJSON geometry dict; ``shape``
    lazily builds and caches the equivalent shapely geometry for computation."""

    layer_id: str
    feature_key: str
    properties: dict[str, Any]
    geometry: dict[str, Any]
    source_id: str
    acquired_at: datetime

    @cached_property
    def shape(self) -> Any:
        return shapely_shape(self.geometry)


@dataclass(frozen=True)
class NearestResult:
    """Result of one nearest-feature computation."""

    feature: Feature
    distance_nm: float
    bearing_deg: float | None
    inside: bool
    closest_lat: float
    closest_lon: float


@dataclass
class _LayerIndex:
    features: list[Feature]
    shapes: list[Any]
    tree: STRtree | None
    mtime: float | None


# --------------------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------------------


class VectorStore:
    """Vector feature store. Selects PostGIS when reachable, otherwise the file backend.

    The file backend is always fully functional; PostGIS is queried through the exact
    same shapely-based geometry code after reading rows back, so behaviour is identical
    regardless of which backend is active — only durability/location differs.
    """

    def __init__(self, dsn: str | None = None) -> None:
        self._index_cache: dict[str, _LayerIndex] = {}
        self._conn: Any | None = None
        self._backend: str = "file"
        resolved = dsn or env("FORESHORE_PG_DSN")
        if resolved:
            conn = _probe_postgis(resolved)
            if conn is not None:
                self._conn = conn
                self._backend = "postgis"

    @property
    def backend(self) -> str:
        return self._backend

    # -- paths (file backend) -----------------------------------------------------------

    def _file_path(self, layer_id: str) -> Path:
        return STATIC_DIR / f"{layer_id}.geojson"

    def _meta_path(self, layer_id: str) -> Path:
        return STATIC_DIR / f"{layer_id}.meta.json"

    # -- indexing -------------------------------------------------------------------------

    def _build_index(self, feats: list[Feature], mtime: float | None) -> _LayerIndex:
        shapes = [f.shape for f in feats]
        tree = STRtree(shapes) if shapes else None
        return _LayerIndex(features=feats, shapes=shapes, tree=tree, mtime=mtime)

    def _get_index(self, layer_id: str) -> _LayerIndex:
        if self._backend == "postgis":
            feats = self._pg_read_layer(layer_id)
            return self._build_index(feats, mtime=None)
        path = self._file_path(layer_id)
        if not path.exists():
            return self._build_index([], mtime=None)
        mtime = path.stat().st_mtime
        cached = self._index_cache.get(layer_id)
        if cached is not None and cached.mtime == mtime:
            return cached
        feats = self._read_file_layer(layer_id)
        idx = self._build_index(feats, mtime=mtime)
        self._index_cache[layer_id] = idx
        return idx

    # -- file backend I/O -----------------------------------------------------------------

    def _read_file_layer(self, layer_id: str) -> list[Feature]:
        path = self._file_path(layer_id)
        if not path.exists():
            return []
        raw = orjson.loads(path.read_bytes())
        feats: list[Feature] = []
        for rec in raw.get("features", []):
            acquired_raw = rec.get("acquired_at")
            acquired_at = _parse_dt(acquired_raw) if acquired_raw else _ensure_aware(
                datetime.now(UTC)
            )
            feats.append(
                Feature(
                    layer_id=layer_id,
                    feature_key=rec.get("feature_key") or "",
                    properties=dict(rec.get("properties") or {}),
                    geometry=rec.get("geometry"),
                    source_id=rec.get("source_id") or "",
                    acquired_at=acquired_at,
                )
            )
        return feats

    def _write_file_layer(
        self,
        layer_id: str,
        features: list[dict[str, Any]],
        source_id: str,
        acquired_at: datetime,
        key_property: str | None,
    ) -> int:
        path = self._file_path(layer_id)
        existing: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        if path.exists():
            try:
                raw = orjson.loads(path.read_bytes())
                for rec in raw.get("features", []):
                    fk = rec.get("feature_key") or _feature_key(rec, key_property, len(order))
                    if fk not in existing:
                        order.append(fk)
                    existing[fk] = rec
            except Exception:
                existing, order = {}, []

        acquired_iso = _ensure_aware(acquired_at)
        written = 0
        for i, feat in enumerate(features):
            fk = _feature_key(feat, key_property, i)
            record = {
                "type": "Feature",
                "feature_key": fk,
                "properties": dict(feat.get("properties") or {}),
                "geometry": feat.get("geometry"),
                "source_id": source_id,
                "acquired_at": acquired_iso,
            }
            if fk not in existing:
                order.append(fk)
            existing[fk] = record
            written += 1

        STATIC_DIR.mkdir(parents=True, exist_ok=True)
        out = {"type": "FeatureCollection", "features": [existing[k] for k in order]}
        path.write_bytes(orjson.dumps(out))
        meta = {
            "source_id": source_id,
            "acquired_at": acquired_iso,
            "feature_count": len(order),
        }
        self._meta_path(layer_id).write_bytes(orjson.dumps(meta))
        self._index_cache.pop(layer_id, None)
        return written

    # -- postgis backend I/O --------------------------------------------------------------

    def _pg_write_layer(
        self,
        layer_id: str,
        features: list[dict[str, Any]],
        source_id: str,
        acquired_at: datetime,
        key_property: str | None,
    ) -> int:
        acquired_iso = _ensure_aware(acquired_at).isoformat()
        n = 0
        assert self._conn is not None
        with self._conn.cursor() as cur:
            for i, feat in enumerate(features):
                props = feat.get("properties") or {}
                geom = feat.get("geometry")
                fkey = _feature_key(feat, key_property, i)
                cur.execute(
                    """
                    INSERT INTO features (layer_id, feature_key, properties, geom, source_id, acquired_at)
                    VALUES (%s, %s, %s::jsonb, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s, %s)
                    ON CONFLICT (layer_id, feature_key) DO UPDATE SET
                        properties = EXCLUDED.properties,
                        geom = EXCLUDED.geom,
                        source_id = EXCLUDED.source_id,
                        acquired_at = EXCLUDED.acquired_at
                    """,
                    (
                        layer_id,
                        fkey,
                        json.dumps(props, default=str),
                        json.dumps(geom),
                        source_id,
                        acquired_iso,
                    ),
                )
                n += 1
        return n

    def _pg_read_layer(self, layer_id: str) -> list[Feature]:
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT feature_key, properties, ST_AsGeoJSON(geom), source_id, acquired_at "
                "FROM features WHERE layer_id = %s",
                (layer_id,),
            )
            rows = cur.fetchall()
        feats: list[Feature] = []
        for feature_key, properties, geom_json, source_id, acquired_at in rows:
            props = properties if isinstance(properties, dict) else json.loads(properties or "{}")
            feats.append(
                Feature(
                    layer_id=layer_id,
                    feature_key=feature_key,
                    properties=props,
                    geometry=json.loads(geom_json),
                    source_id=source_id,
                    acquired_at=_parse_dt(acquired_at),
                )
            )
        return feats

    def _pg_layers(self) -> list[str]:
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute("SELECT DISTINCT layer_id FROM features ORDER BY layer_id")
            return [r[0] for r in cur.fetchall()]

    def _pg_layer_meta(self, layer_id: str) -> dict[str, Any]:
        assert self._conn is not None
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM features WHERE layer_id = %s", (layer_id,))
            count = cur.fetchone()[0]
            cur.execute(
                "SELECT source_id, acquired_at FROM features WHERE layer_id = %s "
                "ORDER BY acquired_at DESC LIMIT 1",
                (layer_id,),
            )
            row = cur.fetchone()
        source_id = row[0] if row else None
        acquired_at = _parse_dt(row[1]).isoformat() if row and row[1] else None
        return {"source_id": source_id, "acquired_at": acquired_at, "count": count}

    # -- public API -------------------------------------------------------------------------

    def write_layer(
        self,
        layer_id: str,
        features: list[dict[str, Any]],
        source_id: str,
        acquired_at: datetime,
        key_property: str | None = None,
    ) -> int:
        """Idempotent upsert of ``features`` into ``layer_id``. Returns the number of
        input features written (not the resulting total layer size)."""
        if self._backend == "postgis":
            return self._pg_write_layer(layer_id, features, source_id, acquired_at, key_property)
        return self._write_file_layer(layer_id, features, source_id, acquired_at, key_property)

    def layers(self) -> list[str]:
        if self._backend == "postgis":
            return self._pg_layers()
        if not STATIC_DIR.exists():
            return []
        return sorted(p.stem for p in STATIC_DIR.glob("*.geojson"))

    def read_layer(self, layer_id: str) -> list[Feature]:
        return self._get_index(layer_id).features

    def layer_meta(self, layer_id: str) -> dict[str, Any]:
        if self._backend == "postgis":
            return self._pg_layer_meta(layer_id)
        mpath = self._meta_path(layer_id)
        if not mpath.exists():
            idx = self._get_index(layer_id)
            return {"source_id": None, "acquired_at": None, "count": len(idx.features)}
        raw = orjson.loads(mpath.read_bytes())
        return {
            "source_id": raw.get("source_id"),
            "acquired_at": raw.get("acquired_at"),
            "count": raw.get("feature_count", 0),
        }

    def _candidate_indices(
        self, idx: _LayerIndex, pt: Point, want: int, max_nm: float | None
    ) -> list[int]:
        total = len(idx.shapes)
        if idx.tree is None or total == 0:
            return []
        if max_nm is not None:
            # Generous degree buffer: nm -> deg with a longitude-compression correction
            # and a safety margin. Only ever used to narrow candidates; the reported
            # distance always comes from the exact haversine measurement below.
            lat_cos = max(math.cos(math.radians(pt.y)), 0.05)
            buf = max((max_nm / 60.0) * 1.6 / lat_cos, 0.02)
            box_geom = shapely_box(pt.x - buf, pt.y - buf, pt.x + buf, pt.y + buf)
            hits = idx.tree.query(box_geom)
            return sorted({int(i) for i in hits})
        buf = 0.05
        seen: set[int] = set()
        for _ in range(14):
            box_geom = shapely_box(pt.x - buf, pt.y - buf, pt.x + buf, pt.y + buf)
            hits = idx.tree.query(box_geom)
            seen = {int(i) for i in hits}
            if len(seen) >= min(want, total):
                # One more, wider pass to catch true near-boundary neighbours the
                # square search box might have just missed.
                buf *= 2.0
                box_geom = shapely_box(pt.x - buf, pt.y - buf, pt.x + buf, pt.y + buf)
                seen |= {int(i) for i in idx.tree.query(box_geom)}
                break
            if buf >= 360.0:
                break
            buf *= 4.0
        if not seen:
            seen = set(range(total))
        return sorted(seen)

    def nearest(
        self, layer_id: str, lat: float, lon: float, *, n: int = 1, max_nm: float | None = None
    ) -> list[NearestResult]:
        idx = self._get_index(layer_id)
        if not idx.features:
            return []
        pt = Point(lon, lat)
        candidates = self._candidate_indices(idx, pt, want=max(n, 1), max_nm=max_nm)
        results: list[NearestResult] = []
        for i in candidates:
            feat = idx.features[i]
            shp = idx.shapes[i]
            inside = shp.geom_type in _POLYGONAL and shp.contains(pt)
            if inside:
                dist_nm = 0.0
                c_lat, c_lon = lat, lon
            else:
                _, closest = nearest_points(pt, shp)
                c_lat, c_lon = closest.y, closest.x
                dist_nm = haversine_nm(lat, lon, c_lat, c_lon)
            if max_nm is not None and dist_nm > max_nm:
                continue
            brg = None if dist_nm <= 0 else _bearing_deg(lat, lon, c_lat, c_lon)
            results.append(
                NearestResult(
                    feature=feat,
                    distance_nm=dist_nm,
                    bearing_deg=brg,
                    inside=inside,
                    closest_lat=c_lat,
                    closest_lon=c_lon,
                )
            )
        results.sort(key=lambda r: r.distance_nm)
        return results[:n]

    def contains(self, layer_id: str, lat: float, lon: float) -> list[Feature]:
        idx = self._get_index(layer_id)
        if not idx.shapes:
            return []
        pt = Point(lon, lat)
        if idx.tree is not None:
            candidates = [int(i) for i in idx.tree.query(pt, predicate="within")]
        else:
            candidates = list(range(len(idx.shapes)))
        out: list[Feature] = []
        for i in candidates:
            shp = idx.shapes[i]
            if shp.geom_type in _POLYGONAL and shp.contains(pt):
                out.append(idx.features[i])
        return out

    def intersecting_bbox(
        self, layer_id: str, bbox: tuple[float, float, float, float]
    ) -> list[Feature]:
        idx = self._get_index(layer_id)
        if not idx.shapes:
            return []
        minlon, minlat, maxlon, maxlat = bbox
        query_box = shapely_box(minlon, minlat, maxlon, maxlat)
        if idx.tree is not None:
            candidates = [int(i) for i in idx.tree.query(query_box, predicate="intersects")]
        else:
            candidates = list(range(len(idx.shapes)))
        return [idx.features[i] for i in candidates if idx.shapes[i].intersects(query_box)]

    def as_geojson(self, layer_id: str) -> dict[str, Any]:
        idx = self._get_index(layer_id)
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "feature_key": f.feature_key,
                    "properties": dict(f.properties),
                    "geometry": f.geometry,
                    "source_id": f.source_id,
                    "acquired_at": f.acquired_at.isoformat(),
                }
                for f in idx.features
            ],
        }


__all__ = ["Feature", "NearestResult", "VectorStore"]
