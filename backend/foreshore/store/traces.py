"""Persistent reasoning-trace store.

Explainability is a stored artifact, not post-hoc LLM narration — this is what the shore
console's trace inspector reads, and what a judge can audit after the fact.

Two backends, one authority:

- **JSONL file** — ``data/cache/traces/traces.jsonl`` by default. Always written, always
  read from. Every read in this module comes from the file, never from Postgres, which
  is what keeps the demo alive with Docker down.
- **Postgres** (``traces`` table, ``scripts/sql/init.sql``) — written to *in addition*,
  only when ``FORESHORE_PG_DSN`` is set, ``psycopg`` imports, and the server actually
  answers at construction time. An accelerator for the console's SQL queries, never a
  dependency. A failure at write time (connection dropped mid-demo, table missing)
  silently drops the Postgres leg for the rest of the process; the JSONL leg is
  unaffected and nothing raises.

Trace shape: one :class:`~foreshore.models.TraceStep` per node. ``parent_id`` links a
step to the step that spawned it, forming a tree the console renders as a reasoning
graph. :meth:`TraceStore.tree` builds that nesting defensively — a step with a missing
or cyclic parent still appears in the output exactly once; it just surfaces as its own
root instead of being dropped or hanging the inspector.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import orjson

from ..config import CACHE_DIR, env
from ..models import UTC, TraceStep, utcnow

_DEFAULT_PATH = CACHE_DIR / "traces" / "traces.jsonl"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _probe_pg(dsn: str) -> Any | None:
    """Try to open a live Postgres connection and confirm the ``traces`` table exists.

    Any failure (missing driver, unreachable server, missing table) degrades silently
    to ``None`` — the caller falls back to JSONL-only, which is always fully functional
    on its own.
    """
    try:
        import psycopg  # type: ignore
    except ImportError:
        return None
    try:
        conn = psycopg.connect(dsn, connect_timeout=2, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM traces LIMIT 1")
        return conn
    except Exception:
        return None


def _pg_insert(conn: Any, step: TraceStep) -> None:
    payload = {
        "args": step.args,
        "result_digest": step.result_digest,
        "provenance_ids": step.provenance_ids,
        "duration_ms": step.duration_ms,
        "why": step.why,
        "ok": step.ok,
        "error": step.error,
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO traces (step_id, query_id, parent_id, agent, kind, tool, payload, ts)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (step_id) DO NOTHING
            """,
            (
                step.step_id,
                step.query_id,
                step.parent_id,
                step.agent,
                step.kind,
                step.tool,
                orjson.dumps(payload, default=str).decode("utf-8"),
                step.ts.isoformat(),
            ),
        )


def _step_from_dict(rec: dict[str, Any]) -> TraceStep:
    ts_raw = rec.get("ts")
    ts = _aware(datetime.fromisoformat(ts_raw)) if ts_raw else utcnow()
    return TraceStep(
        step_id=rec.get("step_id", ""),
        query_id=rec.get("query_id", ""),
        parent_id=rec.get("parent_id"),
        agent=rec.get("agent", ""),
        kind=rec.get("kind", "tool_call"),
        tool=rec.get("tool"),
        args=dict(rec.get("args") or {}),
        result_digest=rec.get("result_digest", ""),
        provenance_ids=list(rec.get("provenance_ids") or []),
        duration_ms=int(rec.get("duration_ms") or 0),
        ts=ts,
        why=rec.get("why"),
        ok=bool(rec.get("ok", True)),
        error=rec.get("error"),
    )


class TraceStore:
    """Reasoning-trace store. JSONL is always the backend of record for reads; Postgres
    (when reachable) is written to in addition, purely as a query accelerator for the
    console."""

    def __init__(self, path: Path | None = None, dsn: str | None = None) -> None:
        self._path = path or _DEFAULT_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Any | None = None
        backends = ["jsonl"]
        resolved_dsn = dsn or env("FORESHORE_PG_DSN")
        if resolved_dsn:
            conn = _probe_pg(resolved_dsn)
            if conn is not None:
                self._conn = conn
                backends.append("postgres")
        self._backends: tuple[str, ...] = tuple(backends)

    @property
    def backends(self) -> tuple[str, ...]:
        return self._backends

    def _drop_postgres(self) -> None:
        self._conn = None
        self._backends = tuple(b for b in self._backends if b != "postgres")

    # -- writes -----------------------------------------------------------------------------

    def _write_lines(self, steps: list[TraceStep]) -> None:
        if not steps:
            return
        lines = "\n".join(orjson.dumps(s.to_dict()).decode("utf-8") for s in steps)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(lines + "\n")
            fh.flush()
            try:
                import os

                os.fsync(fh.fileno())
            except OSError:
                pass

    def append(self, step: TraceStep) -> None:
        self._write_lines([step])
        if self._conn is not None:
            try:
                _pg_insert(self._conn, step)
            except Exception:
                self._drop_postgres()

    def append_many(self, steps: list[TraceStep]) -> None:
        self._write_lines(steps)
        if self._conn is not None:
            for s in steps:
                try:
                    _pg_insert(self._conn, s)
                except Exception:
                    self._drop_postgres()
                    break

    # -- reads ------------------------------------------------------------------------------

    def _read_all(self) -> list[TraceStep]:
        """Parse the JSONL file. A malformed or truncated line (a mid-write crash cut
        the last line off) is skipped rather than raised — a demo must survive a
        half-written trace file."""
        if not self._path.exists():
            return []
        steps: list[TraceStep] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = orjson.loads(line)
                except Exception:
                    continue
                try:
                    steps.append(_step_from_dict(rec))
                except Exception:
                    continue
        return steps

    def get(self, query_id: str) -> list[TraceStep]:
        steps = [s for s in self._read_all() if s.query_id == query_id]
        steps.sort(key=lambda s: s.ts)
        return steps

    def recent_queries(self, limit: int = 20) -> list[dict[str, Any]]:
        by_query: dict[str, list[TraceStep]] = {}
        for s in self._read_all():
            by_query.setdefault(s.query_id, []).append(s)
        rows: list[dict[str, Any]] = []
        for qid, steps in by_query.items():
            steps_sorted = sorted(steps, key=lambda s: s.ts)
            rows.append(
                {
                    "query_id": qid,
                    "started_at": steps_sorted[0].ts.isoformat(),
                    "agents": sorted({s.agent for s in steps}),
                    "step_count": len(steps),
                    "tools": sorted({s.tool for s in steps if s.tool}),
                }
            )
        rows.sort(key=lambda r: r["started_at"], reverse=True)
        return rows[:limit]

    def tree(self, query_id: str) -> list[dict[str, Any]]:
        """Nest this query's steps by ``parent_id`` for the console's reasoning graph.

        A step whose parent is absent (or missing from this query entirely) becomes a
        root. A step caught in a parent cycle can never be reached by descending from a
        genuine root; rather than silently dropping it, any step left unreached after
        the real roots are built is surfaced as its own root too, with recursion capped
        by a visited-set so a cycle can never hang the inspector.
        """
        steps = self.get(query_id)
        by_id = {s.step_id: s for s in steps}
        children: dict[str, list[str]] = {}
        roots: list[str] = []
        for s in steps:
            if s.parent_id and s.parent_id in by_id and s.parent_id != s.step_id:
                children.setdefault(s.parent_id, []).append(s.step_id)
            else:
                roots.append(s.step_id)

        def build(step_id: str, visiting: frozenset[str]) -> dict[str, Any]:
            if step_id in visiting:
                return {"step": by_id[step_id].to_dict(), "children": []}
            nxt = visiting | {step_id}
            return {
                "step": by_id[step_id].to_dict(),
                "children": [build(c, nxt) for c in children.get(step_id, [])],
            }

        result = [build(rid, frozenset()) for rid in roots]

        reached: set[str] = set()

        def collect(node: dict[str, Any]) -> None:
            reached.add(node["step"]["step_id"])
            for c in node["children"]:
                collect(c)

        for node in result:
            collect(node)
        for s in steps:
            if s.step_id not in reached:
                orphan = build(s.step_id, frozenset())
                result.append(orphan)
                collect(orphan)

        return result

    def stats(self) -> dict[str, Any]:
        all_steps = self._read_all()
        tools_used: dict[str, int] = {}
        agents: dict[str, int] = {}
        queries: set[str] = set()
        for s in all_steps:
            queries.add(s.query_id)
            agents[s.agent] = agents.get(s.agent, 0) + 1
            if s.tool:
                tools_used[s.tool] = tools_used.get(s.tool, 0) + 1
        return {
            "queries": len(queries),
            "steps": len(all_steps),
            "tools_used": tools_used,
            "agents": agents,
        }

    def clear(self) -> None:
        if self._path.exists():
            self._path.unlink()
        if self._conn is not None:
            try:
                with self._conn.cursor() as cur:
                    cur.execute("DELETE FROM traces")
            except Exception:
                self._drop_postgres()


# --------------------------------------------------------------------------------------
# Factory + digest
# --------------------------------------------------------------------------------------


def new_step(
    query_id: str,
    agent: str,
    kind: str,
    *,
    tool: str | None = None,
    args: dict[str, Any] | None = None,
    result_digest: str = "",
    provenance_ids: list[str] | None = None,
    duration_ms: int = 0,
    parent_id: str | None = None,
    why: str | None = None,
    ok: bool = True,
    error: str | None = None,
) -> TraceStep:
    """Mint a :class:`~foreshore.models.TraceStep` with a fresh uuid4 ``step_id`` and
    the current UTC timestamp."""
    return TraceStep(
        step_id=str(uuid4()),
        query_id=query_id,
        parent_id=parent_id,
        agent=agent,
        kind=kind,  # type: ignore[arg-type]
        tool=tool,
        args=dict(args or {}),
        result_digest=result_digest,
        provenance_ids=list(provenance_ids or []),
        duration_ms=duration_ms,
        ts=utcnow(),
        why=why,
        ok=ok,
        error=error,
    )


_MAX_DIGEST_LEN = 200
_MAX_STR_LEN = 40
_MAX_LIST_ITEMS = 6


def _reduce(obj: Any) -> Any:
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return str(obj)
        return round(obj, 3)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, str):
        if len(obj) > _MAX_STR_LEN:
            return f"{obj[:_MAX_STR_LEN]}…(len={len(obj)})"
        return obj
    if isinstance(obj, dict):
        return {str(k): _reduce(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        seq = list(obj)
        if len(seq) > _MAX_LIST_ITEMS:
            head = [_reduce(x) for x in seq[:_MAX_LIST_ITEMS]]
            head.append(f"…(+{len(seq) - _MAX_LIST_ITEMS} more)")
            return head
        return [_reduce(x) for x in seq]
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            return _reduce(to_dict())
        except Exception:
            pass
    return _reduce(str(obj))


def digest(obj: Any) -> str:
    """Short, deterministic, human-readable digest of a tool result for the trace.

    At most ~200 characters, JSON-ish, floats rounded to 3 decimal places, long strings
    and long lists elided with a count rather than truncated mid-value. This is a
    summary for the trace inspector, never the payload itself — the full result lives
    in the tool's own return value, not in the trace.
    """
    reduced = _reduce(obj)
    try:
        text = orjson.dumps(reduced, option=orjson.OPT_SORT_KEYS).decode("utf-8")
    except TypeError:
        text = str(reduced)
    if len(text) > _MAX_DIGEST_LEN:
        text = text[: _MAX_DIGEST_LEN - 1] + "…"
    return text


__all__ = ["TraceStore", "new_step", "digest"]
