"""IMD Coastal Weather Bulletin — the advisory ceiling.

FORESHORE may never issue a verdict more permissive than this bulletin. Parsing
correctness here is safety-critical: every extractor in this module is conservative by
construction — a field that cannot be found or a validity line that does not parse comes
back as ``None``, never a guessed value. Downstream ceiling logic treats an unparseable
bulletin the same as a missing one (``DO_NOT_ADVISE``); it must never silently fall back
to "now" or to an empty-but-truthy string.

Endpoint (keyless, verified live)::

    https://mausam.imd.gov.in/Forecast/coastal_bulletin_new.php?id={office_id}

``office_id`` and the coast-block names are read from region config
(``imd_coastal_office_id``, ``imd_bulletin_coast_block``, ``imd_bulletin_fallback_blocks``)
— never hardcoded here, per CLAUDE.md's region-config invariant.

The page is a loose HTML fragment (no closing ``</html>`` guarantees, tabs and stray
newlines inside table cells, compound descriptor strings). All text extraction goes
through :func:`_clean`, which collapses every run of whitespace (including ``&nbsp;``,
tabs, and embedded newlines) to a single space.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from bs4 import BeautifulSoup

from ..config import RegionConfig
from ..models import UTC, Observation, Provenance
from .base import FetchResult, Source, SourceError

#: Normalised (casefolded) cell label -> CoastalBulletin field name.
COAST_FIELD_LABELS: dict[str, str] = {
    "wind": "wind",
    "weather": "weather",
    "visibility": "visibility",
    "sea condition": "sea_condition",
    "port signal": "port_signal",
    "storm surge/tidal warning": "storm_surge_warning",
    "storm surge / tidal warning": "storm_surge_warning",
}

#: CoastalBulletin field name -> Observation.variable name.
_OBS_VARIABLE_BY_FIELD: dict[str, str] = {
    "wind": "wind_description",
    "weather": "weather_description",
    "visibility": "visibility_description",
    "sea_condition": "sea_condition",
    "port_signal": "port_signal",
    "storm_surge_warning": "storm_surge_tidal_warning",
}

_BULLETIN_URL = "https://mausam.imd.gov.in/Forecast/coastal_bulletin_new.php"

_WS_RE = re.compile(r"\s+")

_VALIDITY_RE = re.compile(
    r"Valid\s+for\s+(\d+)\s+hrs\s+from\s+(\d{1,2})\s+UTC\s+of\s+(\d{4}-\d{2}-\d{2})"
    r"\s*to\s*(\d{1,2})\s+UTC\s+of\s+(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)


def _clean(text: str | None) -> str | None:
    """Collapse all whitespace runs (tabs, newlines, ``&nbsp;``) to a single space."""
    if text is None:
        return None
    t = text.replace("\xa0", " ")
    t = _WS_RE.sub(" ", t).strip()
    return t or None


# --------------------------------------------------------------------------------------
# Pure HTML parsing — no I/O, no region lookups, so it is independently testable and
# reusable from both fetch()-derived instances in parse() and bulletin().
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _ParsedPage:
    office_name: str | None
    synoptic_situation: str | None
    valid_from: datetime | None
    valid_to: datetime | None
    blocks: dict[str, dict[str, str]]


def _parse_office_name(soup: BeautifulSoup) -> str | None:
    """The office line looks like ``ACWC CHENNAI / <hindi>`` — a bold centred <p>."""
    for p in soup.find_all("p"):
        txt = _clean(p.get_text(" "))
        if not txt or "/" not in txt:
            continue
        low = txt.lower()
        if "meteorological department" in low or "valid for" in low:
            continue
        candidate = txt.split("/", 1)[0].strip()
        if candidate:
            return candidate
    return None


def _parse_validity(full_text: str) -> tuple[datetime | None, datetime | None]:
    m = _VALIDITY_RE.search(full_text)
    if not m:
        return None, None
    _hrs, from_hr, from_date, to_hr, to_date = m.groups()
    try:
        valid_from = (
            datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=UTC)
            + timedelta(hours=int(from_hr))
        )
        valid_to = (
            datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=UTC)
            + timedelta(hours=int(to_hr))
        )
    except ValueError:
        return None, None
    return valid_from, valid_to


def _parse_synoptic_situation(soup: BeautifulSoup) -> str | None:
    """Lives in the one table on the page with no <caption>."""
    for table in soup.find_all("table"):
        if table.find("caption") is not None:
            continue
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            label = _clean(tds[0].get_text(" "))
            if label and label.casefold() == "synoptic situation":
                return _clean(tds[1].get_text(" "))
    return None


def _parse_blocks(soup: BeautifulSoup) -> dict[str, dict[str, str]]:
    """One captioned <table> per coast block; caption text is the block name."""
    blocks: dict[str, dict[str, str]] = {}
    for table in soup.find_all("table"):
        caption = table.find("caption")
        if caption is None:
            continue
        block_name = _clean(caption.get_text(" "))
        if not block_name:
            continue
        fields: dict[str, str] = {}
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            label = _clean(tds[0].get_text(" "))
            value = _clean(tds[1].get_text(" "))
            if not label or value is None:
                continue
            field_name = COAST_FIELD_LABELS.get(
                label.casefold(), _WS_RE.sub("_", label.casefold())
            )
            fields[field_name] = value
        blocks[block_name] = fields
    return blocks


def _parse_page(html: str) -> _ParsedPage:
    soup = BeautifulSoup(html, "lxml")
    full_text = _clean(soup.get_text(" ")) or ""
    valid_from, valid_to = _parse_validity(full_text)
    return _ParsedPage(
        office_name=_parse_office_name(soup),
        synoptic_situation=_parse_synoptic_situation(soup),
        valid_from=valid_from,
        valid_to=valid_to,
        blocks=_parse_blocks(soup),
    )


def _select_block(
    blocks: dict[str, dict[str, str]], region: RegionConfig
) -> tuple[str | None, dict[str, str] | None, bool]:
    """Exact case-insensitive match on the configured block, then fallbacks in order."""
    configured = region.source("imd_bulletin_coast_block")
    fallbacks = region.source("imd_bulletin_fallback_blocks") or []
    candidates: list[tuple[str, bool]] = []
    if configured:
        candidates.append((str(configured), True))
    candidates.extend((str(fb), False) for fb in fallbacks if fb)
    for name, matched_exactly in candidates:
        target = name.casefold()
        for key in blocks:
            if key.casefold() == target:
                return key, blocks[key], matched_exactly
    return None, None, False


# --------------------------------------------------------------------------------------
# Public contract
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CoastalBulletin:
    """A single office's coastal bulletin, resolved to the region's configured block."""

    office_id: int
    office_name: str | None
    coast_block: str | None
    matched_exactly: bool
    wind: str | None
    weather: str | None
    visibility: str | None
    sea_condition: str | None
    port_signal: str | None
    storm_surge_warning: str | None
    synoptic_situation: str | None
    issued_at: datetime | None
    valid_from: datetime | None
    valid_to: datetime | None
    blocks: dict[str, dict[str, str]]
    provenance: Provenance

    def to_dict(self) -> dict[str, Any]:
        return {
            "office_id": self.office_id,
            "office_name": self.office_name,
            "coast_block": self.coast_block,
            "matched_exactly": self.matched_exactly,
            "wind": self.wind,
            "weather": self.weather,
            "visibility": self.visibility,
            "sea_condition": self.sea_condition,
            "port_signal": self.port_signal,
            "storm_surge_warning": self.storm_surge_warning,
            "synoptic_situation": self.synoptic_situation,
            "issued_at": self.issued_at.isoformat() if self.issued_at else None,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "blocks": self.blocks,
            "provenance": self.provenance.to_dict(),
        }


class IMDCoastalBulletin(Source):
    """Advisory-ceiling source. ``sea_condition`` is the Douglas-mapping input."""

    source_id = "imd_coastal_bulletin"
    source_name = "IMD Coastal Weather Bulletin"
    authority = "IMD"
    base_url = _BULLETIN_URL
    validity = timedelta(hours=12)   # hard bound — expired bulletins authorise nothing
    cache_ttl_s = 900.0
    spatial_resolution_m = None

    def _annotate_office(self, office_name: str | None) -> None:
        base = type(self).source_name
        self.source_name = f"{base} ({office_name})" if office_name else base

    # -- Source contract -----------------------------------------------------------

    def fetch(self, office_id: int | None = None) -> FetchResult:
        oid = office_id if office_id is not None else self.region.source("imd_coastal_office_id")
        if oid is None:
            raise SourceError(
                self.source_id, "no imd_coastal_office_id configured for this region"
            )
        return self.get(self.base_url, params={"id": oid})

    def parse(self, raw: FetchResult, **kw: Any) -> list[Observation]:
        page = _parse_page(raw.text)
        self._annotate_office(page.office_name)
        block_name, block_fields, matched_exactly = _select_block(page.blocks, self.region)
        if block_name is None or not block_fields:
            return []

        valid_time = page.valid_from or raw.acquired_at
        prov = self.provenance(
            raw,
            issued_at=page.valid_from,
            valid_from=page.valid_from,
            valid_to=page.valid_to,
            notes=(
                f"office={page.office_name or 'unknown'}; coast_block={block_name}; "
                f"matched_exactly={matched_exactly}"
            ),
        )

        port = self.region.anchor_ports[0]
        observations: list[Observation] = []
        for field_name, value in block_fields.items():
            variable = _OBS_VARIABLE_BY_FIELD.get(field_name)
            if variable is None:
                continue
            observations.append(
                self.observe(
                    variable,
                    value,
                    "descriptor",
                    port.lat,
                    port.lon,
                    valid_time,
                    prov,
                    coast_block=block_name,
                    office=page.office_name,
                    matched_exactly=matched_exactly,
                )
            )
        return observations

    # -- main entry ------------------------------------------------------------------

    def bulletin(self, office_id: int | None = None) -> CoastalBulletin:
        oid = office_id if office_id is not None else self.region.source("imd_coastal_office_id")
        raw = self.fetch(office_id=oid)
        page = _parse_page(raw.text)
        self._annotate_office(page.office_name)
        block_name, block_fields, matched_exactly = _select_block(page.blocks, self.region)

        notes = f"office={page.office_name or 'unknown'}"
        notes += (
            f"; coast_block={block_name}; matched_exactly={matched_exactly}"
            if block_name
            else "; no configured coast block matched any block on the page"
        )
        prov = self.provenance(
            raw,
            issued_at=page.valid_from,
            valid_from=page.valid_from,
            valid_to=page.valid_to,
            notes=notes,
        )

        fields = block_fields or {}

        def g(name: str) -> str | None:
            return fields.get(name)

        return CoastalBulletin(
            office_id=oid,
            office_name=page.office_name,
            coast_block=block_name,
            matched_exactly=matched_exactly,
            wind=g("wind"),
            weather=g("weather"),
            visibility=g("visibility"),
            sea_condition=g("sea_condition"),
            port_signal=g("port_signal"),
            storm_surge_warning=g("storm_surge_warning"),
            synoptic_situation=page.synoptic_situation,
            issued_at=page.valid_from,
            valid_from=page.valid_from,
            valid_to=page.valid_to,
            blocks=page.blocks,
            provenance=prov,
        )


def get_bulletin(
    region: RegionConfig | None = None, office_id: int | None = None
) -> CoastalBulletin:
    """Module-level convenience: ``get_bulletin()`` for the active region."""
    return IMDCoastalBulletin(region=region).bulletin(office_id=office_id)


__all__ = [
    "COAST_FIELD_LABELS",
    "CoastalBulletin",
    "IMDCoastalBulletin",
    "get_bulletin",
]
