"""In-memory alert store for the push/alert path.

One :class:`AlertStore` instance lives per process, held by ``push/loop.py``'s
background scan over tracked vessel positions. Its whole job is to keep the demo (and a
real deployment) legible: a boat sitting 1.8 nm from a boundary must not spam a fresh
``WARN`` every scan tick, an escalation to ``CRITICAL`` must never be swallowed by that
same dedupe, and an acknowledged alert must be able to fire again if the same hazard is
still there next time the loop looks.

``upsert`` is keyed on :attr:`~foreshore.models.Alert.dedupe_key` (a caller-assembled key
— e.g. ``f"{vessel_id}:{geofence_id}"`` — not decided here) and uses
:data:`foreshore.geofence.classes.ALERT_RANK` for the escalation comparison, so alert
severity ordering has exactly one definition in the codebase.
"""

from __future__ import annotations

from datetime import datetime

from ..geofence.classes import ALERT_RANK
from ..models import Alert, utcnow


class AlertStore:
    """In-memory active-alert table, keyed by ``dedupe_key``. No persistence — a fresh
    process starts with an empty store, which is fine: the push loop re-derives current
    proximity every scan and will re-fire anything still true."""

    #: Bounded rolling log size for :meth:`history` — see that method's docstring.
    _HISTORY_MAX = 500

    def __init__(self) -> None:
        self._active: dict[str, Alert] = {}
        self._history: list[Alert] = []

    def upsert(self, alert: Alert) -> Alert | None:
        """Store ``alert`` under its ``dedupe_key``.

        - No active alert for this key yet -> store and return it (new emission).
        - An active alert exists and ``alert.level`` outranks it (:data:`ALERT_RANK`) ->
          escalation: replace and return it. Escalations are always re-emitted.
        - An active alert exists at the same or a lower rank -> suppress: the stored
          record's fields are still refreshed to ``alert``'s (so distance/eta stay
          current), but ``None`` is returned — nothing new was emitted.

        Every call, regardless of which branch it takes, also appends to the bounded
        history log (see :meth:`history`) — a suppressed duplicate still happened.
        """
        existing = self._active.get(alert.dedupe_key)
        if existing is None:
            self._active[alert.dedupe_key] = alert
            self._record_history(alert)
            return alert
        if ALERT_RANK[alert.level] > ALERT_RANK[existing.level]:
            self._active[alert.dedupe_key] = alert
            self._record_history(alert)
            return alert
        # Same or lower rank: refresh the stored fields (fresh distance/eta reach
        # callers of active_for_vessel/all_active) but this is not a new emission.
        self._active[alert.dedupe_key] = alert
        self._record_history(alert)
        return None

    def _record_history(self, alert: Alert) -> None:
        """Append ``alert`` to the bounded rolling history log, trimming from the front
        once it exceeds :attr:`_HISTORY_MAX` entries."""
        self._history.append(alert)
        overflow = len(self._history) - self._HISTORY_MAX
        if overflow > 0:
            del self._history[:overflow]

    def acknowledge(self, alert_id: str, by: str, when: datetime | None = None) -> Alert | None:
        """Acknowledge the active alert with this ``alert_id``, remove it from the
        active set, and return it. A later fresh occurrence of the same hazard — even at
        the same level — is then treated as new and fires again. Returns ``None`` if no
        active alert has this id."""
        when = when or utcnow()
        for key, existing in list(self._active.items()):
            if existing.alert_id == alert_id:
                existing.acknowledged_at = when
                existing.acknowledged_by = by
                del self._active[key]
                return existing
        return None

    def active_for_vessel(self, vessel_id: str) -> list[Alert]:
        """Active alerts for one vessel, worst-first (see :meth:`all_active`)."""
        return [a for a in self.all_active() if a.vessel_id == vessel_id]

    def all_active(self) -> list[Alert]:
        """Every active alert, worst-first: primarily by :data:`ALERT_RANK` descending,
        then by ``created_at`` ascending (oldest first) within the same level."""
        return sorted(
            self._active.values(),
            key=lambda a: (-ALERT_RANK[a.level], a.created_at),
        )

    def clear(self, dedupe_key: str) -> None:
        """Remove an active entry without acknowledging it — for when a fence stops
        being in range at all. A later re-approach fires fresh, exactly as if this key
        had never been seen."""
        self._active.pop(dedupe_key, None)

    def history(
        self, vessel_id: str | None = None, since: datetime | None = None
    ) -> list[Alert]:
        """Every alert this store has ever upserted, most-recent-first, bounded to the
        last :attr:`_HISTORY_MAX` entries ever recorded.

        Unlike :meth:`all_active` / :meth:`active_for_vessel`, an acknowledged or cleared
        alert still appears here — this is the log a console's "alert history" view (and
        ``GET /api/alerts?active=false``) reads from, distinct from the "currently open"
        table those two methods serve. Optionally filtered to one vessel and/or to
        alerts created at or after ``since``.
        """
        rows = list(self._history)
        if vessel_id is not None:
            rows = [a for a in rows if a.vessel_id == vessel_id]
        if since is not None:
            rows = [a for a in rows if a.created_at >= since]
        rows.sort(key=lambda a: a.created_at, reverse=True)
        return rows


__all__ = ["AlertStore"]
