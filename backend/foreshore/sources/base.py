"""Shared source contract.

Every adapter subclasses :class:`Source`. Three things are non-negotiable and live here
so no adapter can forget them:

* INCOIS and IMD GeoServer 403 without a browser ``User-Agent`` and a plausible
  ``Referer``. The shared client always sends both.
* ``FORESHORE_MODE=fixture`` must open no socket. :meth:`Source.get` enforces that.
* Every successful live fetch is snapshotted, so ``freeze_fixtures.py`` has something
  to promote and a dead endpoint degrades to the last good snapshot rather than to a
  traceback.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Literal, Sequence
from urllib.parse import urlsplit

import httpx

from ..config import RegionConfig, is_fixture, load_region
from ..models import Authority, Freshness, Observation, Provenance, utcnow
from ..store import cache as cache_store
from ..store.cache import CachedPayload, FixtureMissing

log = logging.getLogger("foreshore.sources")

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}


def default_referer(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


class SourceError(RuntimeError):
    """Adapter failed. Callers turn this into a missing input, never into a guess."""

    def __init__(self, source_id: str, message: str, *, status: int | None = None):
        super().__init__(f"[{source_id}] {message}")
        self.source_id = source_id
        self.status = status


@dataclass
class FetchResult:
    """Raw payload plus everything provenance needs."""

    payload: Any
    url: str
    key: str
    acquired_at: datetime
    from_fixture: bool = False
    from_cache: bool = False
    status: int | None = None
    latency_ms: int | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.payload if isinstance(self.payload, str) else str(self.payload)

    @property
    def json(self) -> Any:
        return self.payload


_client: httpx.Client | None = None


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            headers=DEFAULT_HEADERS,
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True,
            verify=False,  # several .gov.in hosts serve incomplete chains
        )
    return _client


def close_client() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


class Source:
    """Base adapter.

    Subclasses set :attr:`source_id`, :attr:`source_name`, :attr:`authority` and
    implement :meth:`fetch` / :meth:`parse`. :meth:`provenance` has a working default
    that subclasses refine when the payload carries an issue time.
    """

    source_id: str = "unnamed"
    source_name: str = "unnamed source"
    authority: Authority = "derived"
    base_url: str = ""
    #: Nominal spatial resolution of the product, metres. None for point/text products.
    spatial_resolution_m: float | None = None
    #: How long a record stays usable. Overridden per source (IMD bulletin = 12 h).
    validity: timedelta = timedelta(hours=12)
    #: Reuse a live snapshot younger than this instead of refetching.
    cache_ttl_s: float = 600.0
    is_derived: bool = False

    def __init__(self, region: RegionConfig | None = None):
        self.region = region or load_region()

    # -- transport ---------------------------------------------------------------------

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        referer: str | None = None,
        headers: dict[str, str] | None = None,
        as_json: bool = False,
        key: str | None = None,
        retries: int = 2,
        cache_ttl_s: float | None = None,
        allow_stale_on_error: bool = True,
    ) -> FetchResult:
        """One fetch, honouring fixture mode, cache, retry and the header requirement."""
        k = key or cache_store.key_for(url, params)

        if is_fixture():
            rec = cache_store.read_fixture(self.source_id, k)
            return FetchResult(
                payload=rec.payload, url=rec.url, key=k,
                acquired_at=rec.fetched_at, from_fixture=True, status=200,
            )

        ttl = self.cache_ttl_s if cache_ttl_s is None else cache_ttl_s
        if ttl > 0:
            hit = cache_store.read_latest_cache(self.source_id, k, ttl)
            if hit is not None:
                return FetchResult(
                    payload=hit.payload, url=hit.url, key=k,
                    acquired_at=hit.fetched_at, from_cache=True, status=200,
                )

        hdrs = dict(headers or {})
        hdrs.setdefault("Referer", referer or default_referer(url))
        last: Exception | None = None
        for attempt in range(retries + 1):
            t0 = time.perf_counter()
            try:
                resp = client().get(url, params=params, headers=hdrs)
                latency = int((time.perf_counter() - t0) * 1000)
                resp.raise_for_status()
                payload: Any
                ctype = resp.headers.get("content-type", "")
                if as_json or "json" in ctype:
                    payload = resp.json()
                else:
                    payload = resp.text
                cache_store.write_snapshot(
                    self.source_id, k, str(resp.url), payload,
                    {"status": resp.status_code, "content_type": ctype, "latency_ms": latency},
                )
                return FetchResult(
                    payload=payload, url=str(resp.url), key=k, acquired_at=utcnow(),
                    status=resp.status_code, latency_ms=latency,
                    headers=dict(resp.headers),
                )
            except Exception as exc:  # noqa: BLE001 - adapters must not leak transport errors
                last = exc
                log.warning("%s fetch attempt %d failed: %s", self.source_id, attempt + 1, exc)
                if attempt < retries:
                    time.sleep(0.6 * (2**attempt))

        if allow_stale_on_error:
            stale = cache_store.read_latest_cache(self.source_id, k, None)
            if stale is not None:
                log.warning("%s serving stale snapshot from %s", self.source_id, stale.fetched_at)
                return FetchResult(
                    payload=stale.payload, url=stale.url, key=k,
                    acquired_at=stale.fetched_at, from_cache=True, status=None,
                )
        status = getattr(getattr(last, "response", None), "status_code", None)
        raise SourceError(self.source_id, f"fetch failed for {url}: {last}", status=status)

    # -- contract ----------------------------------------------------------------------

    def fetch(self, **kwargs: Any) -> FetchResult:
        raise NotImplementedError

    def parse(self, raw: FetchResult, **kwargs: Any) -> list[Observation]:
        raise NotImplementedError

    def provenance(
        self,
        raw: FetchResult,
        *,
        issued_at: datetime | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        spatial_resolution_m: float | None = None,
        notes: str | None = None,
        is_derived: bool | None = None,
    ) -> Provenance:
        issued = issued_at
        vf = valid_from if valid_from is not None else issued
        vt = valid_to
        if vt is None and vf is not None:
            vt = vf + self.validity
        note_bits = [n for n in (notes,) if n]
        if raw.from_fixture:
            note_bits.append("replayed from frozen fixture (FORESHORE_MODE=fixture)")
        elif raw.from_cache:
            note_bits.append("served from local snapshot cache")
        return Provenance(
            source_id=self.source_id,
            source_name=self.source_name,
            authority=self.authority,
            url=raw.url,
            acquired_at=raw.acquired_at,
            issued_at=issued,
            valid_from=vf,
            valid_to=vt,
            spatial_resolution_m=(
                self.spatial_resolution_m if spatial_resolution_m is None else spatial_resolution_m
            ),
            is_derived=self.is_derived if is_derived is None else is_derived,
            notes="; ".join(note_bits) or None,
        )

    # -- convenience -------------------------------------------------------------------

    def observe(
        self,
        variable: str,
        value: float | str,
        unit: str,
        lat: float,
        lon: float,
        valid_time: datetime,
        provenance: Provenance,
        **qualifiers: Any,
    ) -> Observation:
        return Observation(
            variable=variable, value=value, unit=unit, lat=lat, lon=lon,
            valid_time=valid_time, provenance=provenance, qualifiers=qualifiers,
        )

    def health(self) -> dict[str, Any]:
        """Used by scripts/healthcheck.py. Adapters may override for a richer row."""
        t0 = time.perf_counter()
        try:
            obs = self.parse(self.fetch())
            latency = int((time.perf_counter() - t0) * 1000)
            prov = obs[0].provenance if obs else None
            return {
                "source_id": self.source_id,
                "ok": True,
                "count": len(obs),
                "latency_ms": latency,
                "issued_at": prov.issued_at.isoformat() if prov and prov.issued_at else None,
                "freshness": prov.freshness if prov else None,
                "resolution_m": prov.spatial_resolution_m if prov else self.spatial_resolution_m,
                "error": None,
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
            }


def bbox_filter(features: Iterable[dict[str, Any]], bbox: Sequence[float]) -> list[dict[str, Any]]:
    """Cheap bbox intersection on GeoJSON features, done on coordinates only."""
    minlon, minlat, maxlon, maxlat = bbox
    out = []
    for f in features:
        geom = f.get("geometry") or {}
        coords = list(_iter_coords(geom.get("coordinates")))
        if not coords:
            continue
        if any(minlon <= x <= maxlon and minlat <= y <= maxlat for x, y in coords):
            out.append(f)
            continue
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        if max(xs) >= minlon and min(xs) <= maxlon and max(ys) >= minlat and min(ys) <= maxlat:
            out.append(f)
    return out


def _iter_coords(node: Any):
    if node is None:
        return
    if isinstance(node, (list, tuple)):
        if node and isinstance(node[0], (int, float)) and len(node) >= 2:
            yield float(node[0]), float(node[1])
            return
        for child in node:
            yield from _iter_coords(child)


__all__ = [
    "Source", "SourceError", "FetchResult", "client", "close_client", "BROWSER_UA",
    "DEFAULT_HEADERS", "default_referer", "bbox_filter", "FixtureMissing",
]
