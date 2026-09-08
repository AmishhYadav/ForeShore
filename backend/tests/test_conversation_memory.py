"""Conversation memory — the store, and the one rule that governs it.

The rule is that history supplies *context*, never *values*. Most of this file exists to
pin that: a number from turn one must not be able to reach turn three's answer, because it
would read as current and be an hour stale, with nothing in the evidence panel to show it.
That is CLAUDE.md invariants 3 and 4 breached simultaneously, and it is the failure mode
multi-turn support introduces if built carelessly.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from foreshore.store.conversations import (
    MAX_CONTEXT_TURNS,
    ConversationStore,
    ConversationTurn,
    new_turn,
)

T0 = datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    return ConversationStore(path=tmp_path / "conversations.jsonl")


def _turn(session="s1", query="q1", text="is it safe", **kw):
    return new_turn(
        session_id=session, query_id=query, text=text,
        utterance_kind=kw.pop("utterance_kind", "OPERATIONAL"), **kw
    )


# -- the invariant ---------------------------------------------------------------------


def test_a_turn_has_nowhere_to_put_an_observation():
    """The guard is structural, not a convention someone has to remember.

    If a future change adds an `observations`/`evidence` field here, a stale number gains
    a route into a later answer with no fresh provenance. There must be no such field.
    """
    fields = {f.name for f in dataclasses.fields(ConversationTurn)}
    for banned in ("observations", "evidence", "readings", "values", "payload"):
        assert banned not in fields, f"ConversationTurn must not carry {banned!r}"


def test_a_turn_carries_the_verdict_label_but_not_the_readings_behind_it():
    """`verdict_level` is a label so a follow-up "why?" is understood. Re-deriving the
    verdict still re-fetches every number with its own provenance."""
    t = _turn(verdict_level="GO_WITH_CAUTION")
    assert t.verdict_level == "GO_WITH_CAUTION"
    assert all(
        not isinstance(v, (list, tuple)) or k == "intents"
        for k, v in dataclasses.asdict(t).items()
    )


def test_round_trip_through_json_preserves_only_context_fields():
    t = _turn(lat=9.3, lon=79.2, when=T0, vessel_class="small_motorised",
              region_id="palk_bay_gom", verdict_level="GO")
    d = t.to_dict()
    assert set(d) == {f.name for f in dataclasses.fields(ConversationTurn)}
    assert d["when"] == T0.isoformat()
    assert isinstance(d["ts"], str)


# -- store behaviour -------------------------------------------------------------------


def test_an_unknown_session_is_a_new_conversation_not_an_error(store):
    """The normal case on a first request. It must never look like a failure."""
    assert store.history("never-seen") == []
    assert store.last("never-seen") is None


def test_history_is_oldest_first_and_scoped_to_one_session(store):
    store.append(_turn(session="s1", query="q1", text="first"))
    store.append(_turn(session="s1", query="q2", text="second"))
    store.append(_turn(session="s2", query="q3", text="other session"))

    assert [t.text for t in store.history("s1")] == ["first", "second"]
    assert [t.text for t in store.history("s2")] == ["other session"]


def test_last_returns_the_most_recent_turn(store):
    store.append(_turn(query="q1", text="first"))
    store.append(_turn(query="q2", text="second"))
    assert store.last("s1").text == "second"


def test_history_is_bounded(store):
    """A reference more than a few turns back is far more likely to be a coincidence of
    wording than a real one, and resolving it would put a position or time into a question
    the user never meant."""
    for i in range(MAX_CONTEXT_TURNS + 4):
        store.append(_turn(query=f"q{i}", text=f"turn {i}"))
    assert len(store.history("s1")) == MAX_CONTEXT_TURNS
    assert store.history("s1")[-1].text == f"turn {MAX_CONTEXT_TURNS + 3}"


def test_a_truncated_final_line_is_skipped_not_raised(store):
    """A mid-write crash cuts the last line off. Same posture as TraceStore._read_all —
    a demo must survive a half-written file."""
    store.append(_turn(query="q1", text="good line"))
    with store._path.open("a", encoding="utf-8") as fh:
        fh.write('{"session_id": "s1", "quer')

    assert [t.text for t in store.history("s1")] == ["good line"]


def test_sessions_lists_most_recently_active_first(store):
    store.append(_turn(session="old", query="q1"))
    store.append(_turn(session="new", query="q2"))
    assert store.sessions()[0] == "new"


def test_clear_empties_the_store(store):
    store.append(_turn())
    store.clear()
    assert store.history("s1") == []


def test_reads_come_back_from_disk_not_from_memory(tmp_path):
    """JSONL is the record of authority — a second process must see the same history."""
    path = tmp_path / "conversations.jsonl"
    ConversationStore(path=path).append(_turn(text="written by the first store"))
    assert [t.text for t in ConversationStore(path=path).history("s1")] == [
        "written by the first store"
    ]


def test_naive_timestamps_round_trip_as_utc_aware(store):
    """Mixed aware/naive datetimes are how ordering bugs get in."""
    store.append(
        dataclasses.replace(_turn(), ts=datetime(2026, 9, 8, 5, 0), when=T0 - timedelta(hours=1))
    )
    turn = store.last("s1")
    assert turn.ts.tzinfo is not None
    assert turn.when.tzinfo is not None
