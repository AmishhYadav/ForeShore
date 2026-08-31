"""Tool package.

Importing this package registers every tool onto the process-wide
:data:`foreshore.tools.registry.registry`. Modules are imported defensively: a tool
module whose upstream adapter is unavailable must not take the whole registry down with
it, because a partially-available system still has to answer — usually by abstaining
with a named handoff, which is a designed outcome rather than an error.
"""

from __future__ import annotations

import importlib
import logging

from .registry import ToolRegistry, ToolSpec, latlon_schema, registry

log = logging.getLogger("foreshore.tools")

#: Tool modules, in registration order. Add a module here and it is live.
TOOL_MODULES: tuple[str, ...] = (
    "advisory",       # 1  get_governing_advisory
    "sea_state",      # 2  get_sea_state
    "weather",        # 3  get_weather, 4 get_lightning_nowcast
    "tide",           # 5  get_tide, 6 get_currents
    "pfz",            # 7  find_nearest_pfz
    "pfz_derived",    # 8  derive_pfz_zones
    "geofence_tools",  # 9  check_geofences, 10 get_exclusion_zones
    "routing_tools",  # 11 plan_route
    "hazards",        # 12 get_hazard_alerts
    "productivity",   # 13 get_productivity_history
    "harbour",        # 14 nearest_harbour
    "verdict_tools",  # 15 evaluate_verdict
    "discovery",      # 16 list_available_data
)

_FAILED: dict[str, str] = {}


def load_all(strict: bool = False) -> ToolRegistry:
    for name in TOOL_MODULES:
        try:
            importlib.import_module(f"{__name__}.{name}")
        except Exception as exc:  # noqa: BLE001 — see module docstring
            _FAILED[name] = f"{type(exc).__name__}: {exc}"
            log.warning("tool module %s unavailable: %s", name, exc)
            if strict:
                raise
    return registry


def failed_modules() -> dict[str, str]:
    return dict(_FAILED)


load_all()

__all__ = [
    "registry", "ToolRegistry", "ToolSpec", "latlon_schema",
    "load_all", "failed_modules", "TOOL_MODULES",
]
