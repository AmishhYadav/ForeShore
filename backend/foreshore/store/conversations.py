"""Conversation memory — what was said before, and nothing that was measured.

The problem statement asks for "contextual, multi-turn conversations that enable users to
refine queries". ``POST /api/query`` is stateless: there is no session id anywhere in the
system, so "what about tomorrow morning?" arrives with no idea what *it* refers to.

This store is the memory. It follows ``store/traces.py``'s proven shape exactly — JSONL is
the record of authority, a malformed final line is skipped rather than raised, and the
whole thing works with no database. Postgres is deliberately **not** mirrored here, unlike
traces: nothing queries conversation history except the planner, one session at a time, so
there is no query to accelerate and ``DECISIONS.md`` D5 says Postgres earns its place by
being an accelerator or not at all.

The one rule that matters
-------------------------

**History supplies context, never values.**

A :class:`ConversationTurn` carries where the boat was, what class it is, what time was
being asked about and what the verdict *was* — the things needed to resolve "what about
tomorrow?" into a complete question. It carries **no** ``Observation``, and there is
deliberately no field it could be put in.

That is not tidiness, it is invariants 3 and 4 at once. A wave height from turn one,
re-used in turn three's answer, is a number with no fresh provenance presented as current.
It would read exactly like a live reading and be an hour stale, and nothing in the evidence
panel would show it. So the number does not survive the turn — only the question's shape
does, and turn three re-fetches with its own provenance.

``verdict_level`` is the one apparent exception and is not one: it is a label
(``GO_WITH_CAUTION``), never the readings behind it, and it exists so a follow-up can say
"why?" and be understood. Re-deriving the verdict still re-fetches everything.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import orjson

from ..config import CACHE_DIR
from ..models import UTC, VerdictLevel, utcnow

_DEFAULT_PATH = CACHE_DIR / "conversations" / "conversations.jsonl"

#: How many turns back the planner may look. A fisherman refining a question refers to the
#: last thing said, occasionally the one before; beyond that a "reference" is far more
#: likely to be a coincidence of wording than a real one, and resolving it would put a
#: position or a time into a question the user did not mean. Bounded on purpose.
MAX_CONTEXT_TURNS = 5


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ConversationTurn:
    """One utterance and the context it established — never the values it reported.

    Lives here rather than in ``models.py`` because it never crosses the API boundary:
    the wire carries only ``session_id``, and this record is read by exactly one caller,
    the planner's follow-up resolver.
    """

    session_id: str
    query_id: str
    ts: datetime
    text: str
    utterance_kind: str
    intents: tuple[str, ...] = ()
    answer_kind: str = "ADVISORY"
    #: Context carried forward. Every one of these is a *question shape*, not a reading.
    lat: float | None = None
    lon: float | None = None
    when: datetime | None = None
    vessel_class: str | None = None
    region_id: str | None = None
    #: The verdict's label only. Never the observations that produced it.
    verdict_level: VerdictLevel | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        d["when"] = self.when.isoformat() if self.when else None
        d["intents"] = list(self.intents)
        return d


def _turn_from_dict(rec: dict[str, Any]) -> ConversationTurn:
    when = rec.get("when")
    return ConversationTurn(
        session_id=rec["session_id"],
        query_id=rec["query_id"],
        ts=_aware(datetime.fromisoformat(rec["ts"])),
        text=rec.get("text", ""),
        utterance_kind=rec.get("utterance_kind", "OPERATIONAL"),
        intents=tuple(rec.get("intents") or ()),
        answer_kind=rec.get("answer_kind", "ADVISORY"),
        lat=rec.get("lat"),
        lon=rec.get("lon"),
        when=_aware(datetime.fromisoformat(when)) if when else None,
        vessel_class=rec.get("vessel_class"),
        region_id=rec.get("region_id"),
        verdict_level=rec.get("verdict_level"),
    )


class ConversationStore:
    """Append-only conversation memory. JSONL is the backend of record."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _DEFAULT_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        #: The push loop and concurrent specialists both write elsewhere, but two browser
        #: tabs on one session can land together here. A multi-line append is not atomic.
        self._lock = threading.Lock()

    # -- writes -------------------------------------------------------------------------

    def append(self, turn: ConversationTurn) -> None:
        line = orjson.dumps(turn.to_dict(), default=str).decode("utf-8")
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                try:
                    import os

                    os.fsync(fh.fileno())
                except OSError:
                    pass

    # -- reads --------------------------------------------------------------------------

    def _read_all(self) -> list[ConversationTurn]:
        """Parse the JSONL file, skipping any malformed or truncated line.

        Same posture as ``TraceStore._read_all``: a mid-write crash cuts the last line off,
        and a demo must survive a half-written file rather than raising on read.
        """
        if not self._path.exists():
            return []
        turns: list[ConversationTurn] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    turns.append(_turn_from_dict(orjson.loads(line)))
                except Exception:
                    continue
        return turns

    def history(self, session_id: str, limit: int = MAX_CONTEXT_TURNS) -> list[ConversationTurn]:
        """The most recent ``limit`` turns for one session, oldest first.

        An unknown session id returns ``[]`` — a new conversation, not an error. That is
        the normal case on a first request and must never look like a failure.
        """
        if not session_id:
            return []
        turns = [t for t in self._read_all() if t.session_id == session_id]
        turns.sort(key=lambda t: t.ts)
        return turns[-limit:] if limit and limit > 0 else turns

    def last(self, session_id: str) -> ConversationTurn | None:
        """The single most recent turn, which is what follow-up resolution actually needs."""
        turns = self.history(session_id, limit=1)
        return turns[-1] if turns else None

    def sessions(self) -> list[str]:
        """Every session id on file, most recently active first. For the console."""
        latest: dict[str, datetime] = {}
        for t in self._read_all():
            if t.session_id not in latest or t.ts > latest[t.session_id]:
                latest[t.session_id] = t.ts
        return [s for s, _ in sorted(latest.items(), key=lambda kv: kv[1], reverse=True)]

    def clear(self) -> None:
        with self._lock:
            if self._path.exists():
                self._path.unlink()


def new_turn(
    *,
    session_id: str,
    query_id: str,
    text: str,
    utterance_kind: str,
    intents: tuple[str, ...] = (),
    answer_kind: str = "ADVISORY",
    lat: float | None = None,
    lon: float | None = None,
    when: datetime | None = None,
    vessel_class: str | None = None,
    region_id: str | None = None,
    verdict_level: VerdictLevel | None = None,
) -> ConversationTurn:
    """Build a turn, stamping ``ts`` from the wall clock.

    Keyword-only so a caller cannot silently transpose ``lat``/``lon`` or ``text``/
    ``utterance_kind``.
    """
    return ConversationTurn(
        session_id=session_id,
        query_id=query_id,
        ts=utcnow(),
        text=text,
        utterance_kind=utterance_kind,
        intents=tuple(intents),
        answer_kind=answer_kind,
        lat=lat,
        lon=lon,
        when=when,
        vessel_class=vessel_class,
        region_id=region_id,
        verdict_level=verdict_level,
    )


__all__ = [
    "ConversationTurn",
    "ConversationStore",
    "new_turn",
    "MAX_CONTEXT_TURNS",
]
