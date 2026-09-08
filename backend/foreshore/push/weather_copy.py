"""Weather/lightning push-alert copy.

Deliberately the same shape as :mod:`foreshore.geofence.classes`'s ``format_copy`` /
``title_for`` pair, over ``config/weather_alerts.yaml`` instead of ``config/geofence.yaml``
-- one trigger ("significant_wave_height", "wind", "lightning"), one level ("WARN" /
"CRITICAL"; there is no "inside" a wind or wave threshold, so no BREACH band exists here),
one language, one template. Copy lives in config, not in this module, for the same reason
the geofence copy does: a region swap must re-home the wording along with the geometry,
and CLAUDE.md is explicit that this text reaches a fisherman's screen verbatim, so it is
never hand-built out of an enum code or a variable name in Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import yaml

from ..config import CONFIG_DIR

WEATHER_ALERTS_PATH = CONFIG_DIR / "weather_alerts.yaml"

#: English-language fallback for an unresolved district name -- keeps a lightning alert
#: legible even when the upstream nowcast could not name one.
_DEFAULT_DISTRICT = {"en": "your area", "ta": "உங்கள் பகுதி"}


@dataclass(frozen=True)
class WeatherCopy:
    """One trigger's resolved copy: a title plus WARN/CRITICAL text, per language."""

    title: dict[str, str]
    warn: dict[str, str]
    critical: dict[str, str]

    def text(self, level: str, lang: str) -> str:
        table = {"WARN": self.warn, "CRITICAL": self.critical}
        # An unmapped level (or a trigger with no WARN block, e.g. lightning) falls back
        # to whichever block the config actually defines rather than raising -- a missing
        # translation must never crash the push loop.
        block = table.get(level) or self.critical or self.warn or {}
        return block.get(lang) or block.get("en", "")


@lru_cache(maxsize=1)
def _load() -> dict[str, WeatherCopy]:
    with WEATHER_ALERTS_PATH.open("r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}
    out: dict[str, WeatherCopy] = {}
    for variable, c in (raw.get("variables") or {}).items():
        out[variable] = WeatherCopy(
            title=c.get("title", {}) or {},
            warn=c.get("warn", {}) or {},
            critical=c.get("critical", {}) or {},
        )
    return out


def title_for(variable: str, lang: str) -> str:
    copy = _load().get(variable)
    if copy is None:
        # Defensive only -- every trigger push/loop.py can raise is defined in
        # config/weather_alerts.yaml. A future trigger added there without config would
        # otherwise crash the tick; this degrades to a readable label instead.
        return variable.replace("_", " ").title()
    return copy.title.get(lang) or copy.title.get("en", variable)


def format_copy(
    variable: str,
    level: str,
    lang: str,
    *,
    value: float | None = None,
    limit: float | None = None,
    district: str | None = None,
) -> str:
    """Render one trigger's copy for a level and language.

    ``value``/``limit`` are always the numbers a real sourced ``Observation`` and a real
    ``config/vessels.yaml`` limit produced -- this function only formats them, it never
    invents one. Unresolved numbers render as "?" rather than a blank or a crash, exactly
    as ``geofence.classes.format_copy`` renders an unknown distance.
    """
    copy = _load().get(variable)
    if copy is None:
        return ""
    template = copy.text(level, lang)
    return template.format(
        value=("?" if value is None else f"{value:.2f}"),
        limit=("?" if limit is None else f"{limit:.2f}"),
        district=district or _DEFAULT_DISTRICT.get(lang, _DEFAULT_DISTRICT["en"]),
    )


__all__ = ["WeatherCopy", "title_for", "format_copy"]
