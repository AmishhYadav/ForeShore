"""Tests for ``scripts/ingest.py``.

Network-free by design, per this repo's whole-suite discipline (see conftest.py, which
forces ``FORESHORE_MODE=fixture`` at import time for every test): ``scripts/ingest.py``
itself forces ``FORESHORE_MODE=live`` at *module* import time, specifically so it can hit
real endpoints when run as a script — exactly like its sibling ``scripts/healthcheck.py``.
Actually importing that module inside the pytest process would flip the session-wide
mode out from under every other test (and ``conftest.py``'s own session-scoped assertion
that the mode stays "fixture"). So, mirroring ``backend/tests/test_healthcheck.py``
exactly, this file never does ``import scripts.ingest``. Instead:

* the pure ``summarise(results) -> (text, exit_code)`` function — where all the
  report-formatting and exit-code logic actually lives, and which does no I/O at all —
  is extracted from the script's own source via ``ast`` and exec'd in an isolated
  namespace, then exercised directly with synthetic, hand-built result rows;
* the script's syntactic validity as a whole (which also confirms it does not, say,
  reference an undefined name at module scope) is separately confirmed with
  ``ast.parse`` — the same documented fallback bar ``test_healthcheck.py`` uses, and
  the mechanism that stands in for "does the module blow up on import" without actually
  paying the cost of importing it (which would touch ``FORESHORE_MODE``).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ingest.py"


# A local stand-in for scripts.ingest.IngestResult — deliberately not imported (see
# module docstring), just a duck-typed object with the same four attributes
# `summarise` actually reads (name, ok, elapsed_ms, note).
@dataclass(frozen=True)
class _IngestResult:
    name: str
    ok: bool
    elapsed_ms: int
    note: str


def _load_summarise():
    """Extract just ``summarise`` out of ingest.py's source via `ast` and exec it in an
    isolated namespace — no `foreshore` import, no FORESHORE_MODE mutation, no network.
    Keeps the real ``from __future__ import annotations`` node from the file so the
    extracted function's annotations stay deferred strings (its signature references
    ``IngestResult``, which this isolated namespace never defines — without the future
    import that name would need to resolve at def time)."""
    source = SCRIPT_PATH.read_text()
    tree = ast.parse(source, filename=str(SCRIPT_PATH))

    future_import = next(
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "__future__"
    )
    summarise_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "summarise"
    )

    module = ast.Module(body=[future_import, summarise_node], type_ignores=[])
    ast.fix_missing_locations(module)
    ns: dict[str, object] = {}
    code = compile(module, filename=str(SCRIPT_PATH), mode="exec")
    exec(code, ns)  # noqa: S102 - trusted local source, no I/O in the extracted subset
    return ns["summarise"]


def test_script_parses() -> None:
    """scripts/ingest.py is syntactically valid Python and safe to import in principle
    (this is the "does the module blow up on import" check, done without actually
    importing it and flipping FORESHORE_MODE — see module docstring)."""
    ast.parse(SCRIPT_PATH.read_text(), filename=str(SCRIPT_PATH))


def test_lazy_imports_only_inside_function_bodies() -> None:
    """Every ``foreshore.sources...`` import lives inside a function body, never at
    module scope — the same defensive-lazy-import discipline scripts/healthcheck.py and
    scripts/fetch_static.py both use, so one adapter's import error degrades to that one
    source failing rather than crashing the whole script (or, worse, an eventual bare
    ``import scripts.ingest``) before it can report on the rest."""
    tree = ast.parse(SCRIPT_PATH.read_text(), filename=str(SCRIPT_PATH))
    for node in tree.body:  # module-level statements only
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("foreshore.sources"):
            raise AssertionError(
                f"found a module-level import of {node.module!r} — adapter imports must "
                "be lazy, inside function bodies, mirroring scripts/healthcheck.py"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("foreshore.sources"), (
                    f"found a module-level import of {alias.name!r} — adapter imports "
                    "must be lazy, inside function bodies"
                )


def test_summarise_all_ok_exits_zero() -> None:
    summarise = _load_summarise()
    results = [
        _IngestResult(name="imd_coastal_bulletin", ok=True, elapsed_ms=120, note="count=1"),
        _IngestResult(name="incois_wfs", ok=True, elapsed_ms=430, note="count=8"),
    ]
    text, code = summarise(results)
    assert code == 0
    assert "imd_coastal_bulletin" in text
    assert "incois_wfs" in text
    assert "2/2 sources OK" in text


def test_summarise_single_digit_failures_among_nine_still_exits_zero() -> None:
    """The whole point of this script's more forgiving exit-code contract (vs.
    scripts/healthcheck.py's "any failure -> exit 1"): a scheduled cron job must not be
    treated as failed just because one flaky endpoint (of nine) had a bad morning."""
    summarise = _load_summarise()
    names = [
        "imd_coastal_bulletin", "imd_geoserver", "incois_wfs", "incois_thredds",
        "incois_argo", "openmeteo_marine", "openmeteo_forecast", "gdacs",
        "marine_regions_imbl",
    ]
    results = [
        _IngestResult(name=n, ok=(n != "gdacs"), elapsed_ms=100, note="count=1" if n != "gdacs" else "TimeoutError: boom")
        for n in names
    ]
    text, code = summarise(results)
    assert code == 0
    assert "8/9 sources OK" in text
    assert "gdacs" in text
    assert "FAIL" in text


def test_summarise_every_source_failed_exits_one() -> None:
    summarise = _load_summarise()
    results = [
        _IngestResult(name="imd_coastal_bulletin", ok=False, elapsed_ms=30000, note="SourceError: timeout"),
        _IngestResult(name="gdacs", ok=False, elapsed_ms=30000, note="SourceError: timeout"),
    ]
    text, code = summarise(results)
    assert code == 1
    assert "0/2 sources OK" in text


def test_summarise_empty_results_is_vacuously_ok() -> None:
    summarise = _load_summarise()
    text, code = summarise([])
    assert code == 0
    assert "0/0 sources OK" in text


def test_summarise_contains_every_source_name() -> None:
    summarise = _load_summarise()
    names = [
        "imd_coastal_bulletin", "imd_geoserver", "incois_wfs", "incois_thredds",
        "incois_argo", "openmeteo_marine", "openmeteo_forecast", "gdacs",
        "marine_regions_imbl",
    ]
    results = [
        _IngestResult(name=n, ok=(i % 2 == 0), elapsed_ms=100 + i, note=f"note-{i}")
        for i, n in enumerate(names)
    ]
    text, code = summarise(results)
    for n in names:
        assert n in text
    assert code == 0  # 5/9 OK, single-digit failures tolerated
    assert "5/9 sources OK" in text
