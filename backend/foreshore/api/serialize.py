"""Shared JSON-shaping helpers for the API layer.

Every response must carry provenance-traceable numbers per ``docs/API.md`` rule 1 — no
route handler may flatten or strip provenance out of a ``ToolResult``/``QueryOutcome``'s
``to_dict()`` on the way to JSON. Most tool payloads are already fully JSON-safe (dicts of
primitives, or objects already ``.to_dict()``'d by the tool module itself), but at least
one — ``plan_route``'s ``payload['route']`` — hands back a live :class:`~foreshore.models.Route`
dataclass instance rather than a dict, because the orchestrator needs that live object to
reuse elsewhere in the same query. :func:`tool_result_response` recursively normalises any
such object via its own ``to_dict()`` so nothing but plain JSON ever reaches the wire,
without every route handler needing to know which tool's payload does this.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import ToolResult


def jsonable(obj: Any) -> Any:
    """Recursively reduce ``obj`` to plain JSON-safe values.

    Dataclasses and other value objects that expose their own ``to_dict()`` are
    converted through it (never re-derived here) so the canonical shape for a given type
    is always defined in exactly one place — the type itself.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return jsonable(to_dict())
    return obj


def tool_result_response(result: ToolResult) -> dict[str, Any]:
    """``ToolResult.to_dict()``, with any live object inside ``payload`` normalised.

    ``observations`` is already fully JSON-safe by construction (``ToolResult.to_dict()``
    calls ``Observation.to_dict()`` for every entry); this only guards ``payload``, the
    one place a tool is free to hand back whatever structure it likes.
    """
    data = result.to_dict()
    data["payload"] = jsonable(data["payload"])
    return data


__all__ = ["jsonable", "tool_result_response"]
