"""FORESHORE core contracts.

Everything downstream depends on these types. Three invariants live here and are
enforced by construction, not by prompt:

1. Every quantitative value the system emits is an :class:`Observation`, and every
   ``Observation`` carries a :class:`Provenance`. There is no way to build one without.
2. ``Verdict`` has exactly three levels. ``DO_NOT_ADVISE`` is a designed outcome and
   requires a named :class:`Handoff`.
3. Freshness is computed from timestamps, never asserted by a caller.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Sequence

UTC = timezone.utc

# --------------------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------------------

Freshness = Literal["live", "recent", "stale", "expired"]

Authority = Literal[
    "IMD",
    "INCOIS",
    "ECMWF/Open-Meteo",
    "JRC/GDACS",
    "VLIZ",
    "GEBCO",
    "ISRO/NRSC",
    "derived",
    "simulated",
]

VerdictLevel = Literal["GO", "GO_WITH_CAUTION", "DO_NOT_ADVISE"]

#: Ordered most permissive -> least permissive. Used for the ceiling comparison.
VERDICT_ORDER: tuple[VerdictLevel, ...] = ("GO", "GO_WITH_CAUTION", "DO_NOT_ADVISE")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Provenance:
    """Where a value came from, when we got it, and how long it may be trusted."""

    source_id: str
    source_name: str
    authority: Authority
    url: str
    acquired_at: datetime
    issued_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    spatial_resolution_m: float | None = None
    temporal_resolution_s: float | None = None
    #: Explicit override. When None, :attr:`freshness` is derived from the timestamps.
    freshness_override: Freshness | None = None
    #: True => an indicative product FORESHORE computed. Never label as an official advisory.
    is_derived: bool = False
    notes: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "acquired_at", _aware(self.acquired_at))
        for f in ("issued_at", "valid_from", "valid_to"):
            object.__setattr__(self, f, _aware(getattr(self, f)))

    # -- freshness ---------------------------------------------------------------------

    def freshness_at(self, now: datetime | None = None) -> Freshness:
        """Derive freshness from validity window and issue age.

        ``expired`` past ``valid_to`` — an expired record cannot authorise anything.
        Otherwise graded on age since ``issued_at`` (falling back to ``acquired_at``)
        relative to the record's own validity span.
        """
        if self.freshness_override is not None:
            return self.freshness_override
        now = _aware(now) or utcnow()
        if self.valid_to is not None and now > self.valid_to:
            return "expired"
        reference = self.issued_at or self.acquired_at
        age = (now - reference).total_seconds()
        if self.valid_from is not None and self.valid_to is not None:
            span = (self.valid_to - self.valid_from).total_seconds()
        else:
            span = 12 * 3600.0
        span = max(span, 60.0)
        if age <= 0.25 * span:
            return "live"
        if age <= 0.75 * span:
            return "recent"
        return "stale"

    @property
    def freshness(self) -> Freshness:
        return self.freshness_at()

    @property
    def age_seconds(self) -> float:
        return (utcnow() - (self.issued_at or self.acquired_at)).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "authority": self.authority,
            "url": self.url,
            "acquired_at": self.acquired_at.isoformat(),
            "issued_at": self.issued_at.isoformat() if self.issued_at else None,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "spatial_resolution_m": self.spatial_resolution_m,
            "temporal_resolution_s": self.temporal_resolution_s,
            "freshness": self.freshness,
            "is_derived": self.is_derived,
            "notes": self.notes,
        }

    @property
    def provenance_id(self) -> str:
        stamp = (self.issued_at or self.acquired_at).isoformat()
        return f"{self.source_id}@{stamp}"


# --------------------------------------------------------------------------------------
# Observation
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """A single sourced value. The only legal carrier of a number in this system."""

    variable: str
    value: float | str
    unit: str
    lat: float
    lon: float
    valid_time: datetime
    provenance: Provenance
    #: Optional extra context (grid cell distance, member spread, raw descriptor, ...).
    qualifiers: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "valid_time", _aware(self.valid_time))
        if not isinstance(self.provenance, Provenance):  # defensive: invariant 1
            raise TypeError("Observation.provenance must be a Provenance record")

    @property
    def is_numeric(self) -> bool:
        return isinstance(self.value, (int, float)) and not isinstance(self.value, bool)

    @property
    def numeric(self) -> float | None:
        return float(self.value) if self.is_numeric else None

    def display(self, decimals: int = 2) -> str:
        if self.is_numeric:
            return f"{float(self.value):.{decimals}f} {self.unit}".strip()
        return str(self.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "variable": self.variable,
            "value": self.value,
            "unit": self.unit,
            "lat": self.lat,
            "lon": self.lon,
            "valid_time": self.valid_time.isoformat(),
            "display": self.display(),
            "qualifiers": self.qualifiers,
            "provenance": self.provenance.to_dict(),
        }


# --------------------------------------------------------------------------------------
# Handoff / Verdict
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Handoff:
    """Named human authority. Required whenever the system abstains."""

    reason: str
    authority_name: str
    authority_type: Literal["landing_centre", "coast_guard", "fisheries_office", "port_office"]
    contact: str | None = None
    lat: float | None = None
    lon: float | None = None
    distance_nm: float | None = None
    provenance: Provenance | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "authority_name": self.authority_name,
            "authority_type": self.authority_type,
            "contact": self.contact,
            "lat": self.lat,
            "lon": self.lon,
            "distance_nm": self.distance_nm,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }


def is_more_permissive(a: VerdictLevel, b: VerdictLevel) -> bool:
    """True when ``a`` allows more than ``b``."""
    return VERDICT_ORDER.index(a) < VERDICT_ORDER.index(b)


def worst_verdict(levels: Iterable[VerdictLevel]) -> VerdictLevel:
    return max(levels, key=VERDICT_ORDER.index, default="DO_NOT_ADVISE")


@dataclass
class Verdict:
    """The advisory. Exactly three levels; ceiling is applied after the LLM has spoken."""

    level: VerdictLevel
    reasons: list[str] = field(default_factory=list)
    evidence: list[Observation] = field(default_factory=list)
    ceiling_applied: bool = False
    ceiling_source: Provenance | None = None
    downgraded_from: VerdictLevel | None = None
    handoff: Handoff | None = None
    #: Human-readable audit of every ceiling rule that fired.
    ceiling_notes: list[str] = field(default_factory=list)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    vessel_class: str | None = None
    lat: float | None = None
    lon: float | None = None

    def validate(self) -> None:
        if self.level not in VERDICT_ORDER:
            raise ValueError(f"illegal verdict level {self.level!r}")
        if self.level == "DO_NOT_ADVISE" and self.handoff is None:
            raise ValueError("DO_NOT_ADVISE requires a named Handoff")
        for obs in self.evidence:
            if not isinstance(obs, Observation):
                raise TypeError("Verdict.evidence must contain Observation records")

    @property
    def provenance_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for obs in self.evidence:
            seen.setdefault(obs.provenance.provenance_id, None)
        return list(seen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "reasons": self.reasons,
            "evidence": [o.to_dict() for o in self.evidence],
            "ceiling_applied": self.ceiling_applied,
            "ceiling_source": self.ceiling_source.to_dict() if self.ceiling_source else None,
            "downgraded_from": self.downgraded_from,
            "ceiling_notes": self.ceiling_notes,
            "handoff": self.handoff.to_dict() if self.handoff else None,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "vessel_class": self.vessel_class,
            "lat": self.lat,
            "lon": self.lon,
        }


# --------------------------------------------------------------------------------------
# Geofence + alerts
# --------------------------------------------------------------------------------------

GeofenceClass = Literal[
    "IMBL_HISTORIC_WATERS",
    "IMBL_MARITIME_BOUNDARY",
    "MPA",
    "ECO_SENSITIVE",
    "USER_DEFINED",
    "HAZARD_EXCLUSION",
]

Severity = Literal["legal_hard", "restricted", "advisory", "hazard"]

AlertLevel = Literal["INFO", "WARN", "CRITICAL", "BREACH"]


@dataclass(frozen=True)
class GeofenceProximity:
    """Result of one vessel-vs-one-geofence computation."""

    geofence_id: str
    geofence_class: GeofenceClass
    name: str
    severity: Severity
    distance_nm: float
    bearing_deg: float | None
    inside: bool
    eta_seconds: float | None
    level: AlertLevel
    provenance: Provenance
    closest_lat: float | None = None
    closest_lon: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "geofence_id": self.geofence_id,
            "geofence_class": self.geofence_class,
            "name": self.name,
            "severity": self.severity,
            "distance_nm": round(self.distance_nm, 3),
            "bearing_deg": self.bearing_deg,
            "inside": self.inside,
            "eta_seconds": self.eta_seconds,
            "level": self.level,
            "closest_lat": self.closest_lat,
            "closest_lon": self.closest_lon,
            "provenance": self.provenance.to_dict(),
        }


@dataclass
class Alert:
    """Push-path emission. Deduped on ``dedupe_key``, acknowledged by the console."""

    alert_id: str
    vessel_id: str
    kind: Literal["geofence", "hazard", "weather", "verdict_change"]
    level: AlertLevel
    title_en: str
    title_ta: str
    body_en: str
    body_ta: str
    lat: float
    lon: float
    created_at: datetime
    dedupe_key: str
    evidence: list[Observation] = field(default_factory=list)
    geofence_class: GeofenceClass | None = None
    distance_nm: float | None = None
    eta_seconds: float | None = None
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    handoff: Handoff | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "vessel_id": self.vessel_id,
            "kind": self.kind,
            "level": self.level,
            "title": {"en": self.title_en, "ta": self.title_ta},
            "body": {"en": self.body_en, "ta": self.body_ta},
            "lat": self.lat,
            "lon": self.lon,
            "created_at": self.created_at.isoformat(),
            "dedupe_key": self.dedupe_key,
            "geofence_class": self.geofence_class,
            "distance_nm": self.distance_nm,
            "eta_seconds": self.eta_seconds,
            "acknowledged_at": (
                self.acknowledged_at.isoformat() if self.acknowledged_at else None
            ),
            "acknowledged_by": self.acknowledged_by,
            "evidence": [o.to_dict() for o in self.evidence],
            "handoff": self.handoff.to_dict() if self.handoff else None,
        }


@dataclass
class VesselState:
    """Tracked vessel. ``is_simulated`` is never hidden — there is no public AIS feed
    for Indian small boats and claiming one would not survive questioning."""

    vessel_id: str
    name: str
    lat: float
    lon: float
    heading_deg: float
    speed_kn: float
    vessel_class: str
    updated_at: datetime
    home_port: str | None = None
    crew: int | None = None
    is_simulated: bool = True
    last_verdict: VerdictLevel | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "vessel_id": self.vessel_id,
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "heading_deg": self.heading_deg,
            "speed_kn": self.speed_kn,
            "vessel_class": self.vessel_class,
            "updated_at": self.updated_at.isoformat(),
            "home_port": self.home_port,
            "crew": self.crew,
            "is_simulated": self.is_simulated,
            "last_verdict": self.last_verdict,
        }


# --------------------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteLeg:
    from_lat: float
    from_lon: float
    to_lat: float
    to_lon: float
    distance_nm: float
    bearing_deg: float
    eta_seconds: float
    #: Per-term cost contributions: base, hs, wind, current, shallow, steep, imbl.
    cost_breakdown: dict[str, float] = field(default_factory=dict)
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": [self.from_lat, self.from_lon],
            "to": [self.to_lat, self.to_lon],
            "distance_nm": round(self.distance_nm, 3),
            "bearing_deg": round(self.bearing_deg, 1),
            "eta_seconds": self.eta_seconds,
            "cost_breakdown": {k: round(v, 4) for k, v in self.cost_breakdown.items()},
            "note": self.note,
        }


@dataclass
class Route:
    waypoints: list[tuple[float, float]]
    legs: list[RouteLeg]
    total_distance_nm: float
    total_eta_seconds: float
    #: Straight-line distance, for the "why does it bend" explanation.
    direct_distance_nm: float
    cost_breakdown: dict[str, float] = field(default_factory=dict)
    evidence: list[Observation] = field(default_factory=list)
    avoided: list[str] = field(default_factory=list)
    feasible: bool = True
    failure_reason: str | None = None
    departure: datetime | None = None
    vessel_class: str | None = None

    @property
    def detour_pct(self) -> float:
        if self.direct_distance_nm <= 0:
            return 0.0
        return 100.0 * (self.total_distance_nm - self.direct_distance_nm) / self.direct_distance_nm

    def to_dict(self) -> dict[str, Any]:
        return {
            "waypoints": [[lat, lon] for lat, lon in self.waypoints],
            "legs": [leg.to_dict() for leg in self.legs],
            "total_distance_nm": round(self.total_distance_nm, 2),
            "direct_distance_nm": round(self.direct_distance_nm, 2),
            "detour_pct": round(self.detour_pct, 1),
            "total_eta_seconds": self.total_eta_seconds,
            "cost_breakdown": {k: round(v, 4) for k, v in self.cost_breakdown.items()},
            "avoided": self.avoided,
            "feasible": self.feasible,
            "failure_reason": self.failure_reason,
            "departure": self.departure.isoformat() if self.departure else None,
            "vessel_class": self.vessel_class,
            "evidence": [o.to_dict() for o in self.evidence],
        }


# --------------------------------------------------------------------------------------
# Agent trace
# --------------------------------------------------------------------------------------


@dataclass
class TraceStep:
    """One node of the stored reasoning trace. Explainability is an artifact, not narration."""

    step_id: str
    query_id: str
    parent_id: str | None
    agent: str
    kind: Literal["plan", "tool_call", "tool_result", "synthesis", "ceiling", "error"]
    tool: str | None
    args: dict[str, Any]
    result_digest: str
    provenance_ids: list[str]
    duration_ms: int
    ts: datetime
    why: str | None = None
    ok: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "query_id": self.query_id,
            "parent_id": self.parent_id,
            "agent": self.agent,
            "kind": self.kind,
            "tool": self.tool,
            "args": self.args,
            "result_digest": self.result_digest,
            "provenance_ids": self.provenance_ids,
            "duration_ms": self.duration_ms,
            "ts": self.ts.isoformat(),
            "why": self.why,
            "ok": self.ok,
            "error": self.error,
        }


@dataclass
class ToolResult:
    """Uniform envelope returned by every tool in the registry.

    ``observations`` is the *only* channel through which numbers reach the LLM. ``payload``
    carries structure (geometry, series, route objects) for the renderer; the synthesis
    guard checks emitted numbers against ``observations``.
    """

    tool: str
    ok: bool
    observations: list[Observation] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    error: str | None = None
    partial: bool = False
    missing: list[str] = field(default_factory=list)

    @property
    def provenance_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for obs in self.observations:
            seen.setdefault(obs.provenance.provenance_id, None)
        return list(seen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "summary": self.summary,
            "observations": [o.to_dict() for o in self.observations],
            "payload": self.payload,
            "error": self.error,
            "partial": self.partial,
            "missing": self.missing,
        }


@dataclass
class AgentAnswer:
    """What the request path returns."""

    query_id: str
    language: str
    text: str
    verdict: Verdict | None
    evidence: list[Observation]
    trace: list[TraceStep]
    route: Route | None = None
    payloads: dict[str, Any] = field(default_factory=dict)
    unsourced_numbers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "language": self.language,
            "text": self.text,
            "verdict": self.verdict.to_dict() if self.verdict else None,
            "evidence": [o.to_dict() for o in self.evidence],
            "trace": [s.to_dict() for s in self.trace],
            "route": self.route.to_dict() if self.route else None,
            "payloads": self.payloads,
            "unsourced_numbers": self.unsourced_numbers,
        }


# --------------------------------------------------------------------------------------
# Geodesy helpers — shared so every module measures distance the same way
# --------------------------------------------------------------------------------------

EARTH_RADIUS_M = 6_371_008.8
NM_PER_M = 1.0 / 1852.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return haversine_m(lat1, lon1, lat2, lon2) * NM_PER_M


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlam)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def project_position(
    lat: float, lon: float, heading_deg_: float, speed_kn: float, seconds: float
) -> tuple[float, float]:
    """Dead-reckon a position forward. Used by the push loop."""
    distance_m = speed_kn * 1852.0 * (seconds / 3600.0)
    theta = math.radians(heading_deg_)
    d_over_r = distance_m / EARTH_RADIUS_M
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d_over_r) + math.cos(p1) * math.sin(d_over_r) * math.cos(theta))
    l2 = l1 + math.atan2(
        math.sin(theta) * math.sin(d_over_r) * math.cos(p1),
        math.cos(d_over_r) - math.sin(p1) * math.sin(p2),
    )
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


__all__ = [
    "UTC", "utcnow", "Freshness", "Authority", "VerdictLevel", "VERDICT_ORDER",
    "Provenance", "Observation", "Handoff", "Verdict", "is_more_permissive", "worst_verdict",
    "GeofenceClass", "Severity", "AlertLevel", "GeofenceProximity", "Alert", "VesselState",
    "RouteLeg", "Route", "TraceStep", "ToolResult", "AgentAnswer",
    "haversine_m", "haversine_nm", "bearing_deg", "project_position",
    "EARTH_RADIUS_M", "NM_PER_M",
]
