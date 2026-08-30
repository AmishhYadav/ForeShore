"""Snapshot cache + fixture replay.

Every source fetch writes ``{payload, fetched_at, url, meta}`` to
``data/cache/<source_id>/<iso8601>.json``. In ``FORESHORE_MODE=fixture`` no socket is
opened at all: adapters replay ``data/fixtures/<source_id>/<key>.json``. This is what
makes the live demo immune to venue wifi, and it is wired from day one because
retrofitting it later costs a day.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import CACHE_DIR, FIXTURE_DIR, is_fixture
from ..models import UTC, utcnow

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slugify(value: str, max_len: int = 80) -> str:
    s = _SAFE.sub("-", value).strip("-")
    if len(s) <= max_len:
        return s or "default"
    digest = hashlib.sha1(value.encode()).hexdigest()[:8]
    return f"{s[: max_len - 9]}-{digest}"


def key_for(url: str, params: dict[str, Any] | None = None) -> str:
    """Stable cache/fixture key for a request. Fixture files are named by this."""
    blob = url if not params else url + "?" + json.dumps(params, sort_keys=True, default=str)
    digest = hashlib.sha1(blob.encode()).hexdigest()[:10]
    tail = url.rstrip("/").split("/")[-1] or "root"
    return f"{slugify(tail, 48)}-{digest}"


class FixtureMissing(FileNotFoundError):
    """Raised in fixture mode when no frozen snapshot exists for a request."""


@dataclass(frozen=True)
class CachedPayload:
    source_id: str
    key: str
    url: str
    payload: Any
    fetched_at: datetime
    meta: dict[str, Any]
    from_fixture: bool = False

    @property
    def is_text(self) -> bool:
        return isinstance(self.payload, str)


def _dir_for(source_id: str, fixture: bool) -> Path:
    root = FIXTURE_DIR if fixture else CACHE_DIR
    return root / slugify(source_id)


def _serialise(payload: Any) -> tuple[str, Any]:
    if isinstance(payload, (dict, list)):
        return "json", payload
    if isinstance(payload, bytes):
        return "b64", payload.hex()
    return "text", str(payload)


def _deserialise(kind: str, blob: Any) -> Any:
    if kind == "b64":
        return bytes.fromhex(blob)
    return blob


def write_snapshot(
    source_id: str,
    key: str,
    url: str,
    payload: Any,
    meta: dict[str, Any] | None = None,
    *,
    fixture: bool = False,
) -> Path:
    d = _dir_for(source_id, fixture)
    d.mkdir(parents=True, exist_ok=True)
    now = utcnow()
    kind, blob = _serialise(payload)
    record = {
        "source_id": source_id,
        "key": key,
        "url": url,
        "fetched_at": now.isoformat(),
        "payload_kind": kind,
        "payload": blob,
        "meta": meta or {},
    }
    # Fixtures are keyed (one canonical file per request); cache is timestamped history.
    name = f"{key}.json" if fixture else f"{key}__{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    path = d / name
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    if not fixture:
        latest = d / f"{key}__latest.json"
        latest.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return path


def _load_record(path: Path, *, from_fixture: bool) -> CachedPayload:
    rec = json.loads(path.read_text(encoding="utf-8"))
    return CachedPayload(
        source_id=rec["source_id"],
        key=rec["key"],
        url=rec["url"],
        payload=_deserialise(rec.get("payload_kind", "json"), rec["payload"]),
        fetched_at=datetime.fromisoformat(rec["fetched_at"]).astimezone(UTC),
        meta=rec.get("meta", {}),
        from_fixture=from_fixture,
    )


def read_fixture(source_id: str, key: str) -> CachedPayload:
    path = _dir_for(source_id, True) / f"{key}.json"
    if not path.exists():
        raise FixtureMissing(
            f"no fixture for source={source_id!r} key={key!r} at {path}. "
            f"Run scripts/freeze_fixtures.py in live mode first."
        )
    return _load_record(path, from_fixture=True)


def read_latest_cache(source_id: str, key: str, max_age_s: float | None = None) -> CachedPayload | None:
    path = _dir_for(source_id, False) / f"{key}__latest.json"
    if not path.exists():
        return None
    rec = _load_record(path, from_fixture=False)
    if max_age_s is not None and (utcnow() - rec.fetched_at).total_seconds() > max_age_s:
        return None
    return rec


def promote_cache_to_fixture(source_id: str, key: str) -> Path | None:
    """Used by scripts/freeze_fixtures.py: copy the newest live snapshot into fixtures."""
    rec = read_latest_cache(source_id, key)
    if rec is None:
        return None
    return write_snapshot(source_id, key, rec.url, rec.payload, rec.meta, fixture=True)


def fixture_available(source_id: str, key: str) -> bool:
    return (_dir_for(source_id, True) / f"{key}.json").exists()


def list_fixtures(source_id: str | None = None) -> list[Path]:
    root = FIXTURE_DIR if source_id is None else _dir_for(source_id, True)
    return sorted(root.glob("**/*.json")) if root.exists() else []


def cache_binary(source_id: str, key: str, data: bytes, suffix: str = ".nc") -> Path:
    """Grids are too large for the JSON record; they land beside it as files."""
    root = (FIXTURE_DIR if is_fixture() else CACHE_DIR) / slugify(source_id) / "blobs"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{key}{suffix}"
    path.write_bytes(data)
    return path


def binary_path(source_id: str, key: str, suffix: str = ".nc") -> Path | None:
    for root in ((FIXTURE_DIR, CACHE_DIR) if is_fixture() else (CACHE_DIR, FIXTURE_DIR)):
        p = root / slugify(source_id) / "blobs" / f"{key}{suffix}"
        if p.exists():
            return p
    return None


__all__ = [
    "CachedPayload", "FixtureMissing", "slugify", "key_for", "write_snapshot", "read_fixture",
    "read_latest_cache", "promote_cache_to_fixture", "fixture_available", "list_fixtures",
    "cache_binary", "binary_path",
]
