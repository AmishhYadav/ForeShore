"""Tests for ``scripts/healthcheck.py``.

Network-free by design, per this repo's whole-suite discipline (see conftest.py, which
forces ``FORESHORE_MODE=fixture`` at import time for every test): ``healthcheck.py``
itself forces ``FORESHORE_MODE=live`` at *module* import time, specifically so it can
hit real endpoints when run as a script — actually importing that module inside the
pytest process would flip the session-wide mode out from under every other test (and
``conftest.py``'s own session-scoped assertion that the mode stays "fixture"). So this
file never does ``import scripts.healthcheck``. Instead:

* the pure ``format_report(results) -> (text, exit_code)`` function — where all the
  report-formatting and exit-code logic actually lives, and which does no I/O at all —
  is extracted from the script's own source via `ast` and exec'd in an isolated
  namespace, then exercised directly with synthetic, hand-built result rows;
* the script's syntactic validity as a whole is separately confirmed with
  ``ast.parse``, the documented fallback bar from the brief for this file, since
  ``scripts/fetch_static.py`` — the sibling script this one mirrors — has no test
  coverage of its own in this suite to pattern-match an import style from (confirmed:
  no test here imports ``fetch_static``).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "healthcheck.py"


# A local stand-in for scripts.healthcheck.SourceCheck — deliberately not imported (see
# module docstring), just a duck-typed object with the same four attributes
# format_report actually reads (name, ok, elapsed_ms, note).
@dataclass(frozen=True)
class _SourceCheck:
    name: str
    ok: bool
    elapsed_ms: int
    note: str


def _load_format_report():
    """Extract just ``format_report`` out of healthcheck.py's source via `ast` and
    exec it in an isolated namespace — no `foreshore` import, no FORESHORE_MODE
    mutation, no network. Keeps the real ``from __future__ import annotations`` node
    from the file so the extracted function's annotations stay deferred strings (it
    references ``SourceCheck`` in its signature, which this isolated namespace never
    defines — without the future import that name would need to resolve at def time)."""
    source = SCRIPT_PATH.read_text()
    tree = ast.parse(source, filename=str(SCRIPT_PATH))

    future_import = next(
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "__future__"
    )
    format_report_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "format_report"
    )

    module = ast.Module(body=[future_import, format_report_node], type_ignores=[])
    ast.fix_missing_locations(module)
    ns: dict[str, object] = {}
    code = compile(module, filename=str(SCRIPT_PATH), mode="exec")
    exec(code, ns)  # noqa: S102 - trusted local source, no I/O in the extracted subset
    return ns["format_report"]


def test_script_parses() -> None:
    """scripts/healthcheck.py is syntactically valid Python.

    scripts/fetch_static.py — the sibling script healthcheck.py mirrors the calling
    convention of — has no test coverage of its own in this suite, so there is no
    existing "how is a repo-root script imported from a test" pattern to follow here;
    this is the documented fallback bar from the brief.
    """
    ast.parse(SCRIPT_PATH.read_text(), filename=str(SCRIPT_PATH))


def test_format_report_all_ok_exits_zero() -> None:
    format_report = _load_format_report()
    results = [
        _SourceCheck(name="imd_coastal_bulletin", ok=True, elapsed_ms=120, note="count=3"),
        _SourceCheck(name="incois_wfs", ok=True, elapsed_ms=430, note="count=8"),
    ]
    text, code = format_report(results)
    assert code == 0
    assert "imd_coastal_bulletin" in text
    assert "incois_wfs" in text
    assert "2/2 sources OK" in text


def test_format_report_any_fail_exits_one() -> None:
    format_report = _load_format_report()
    results = [
        _SourceCheck(name="imd_coastal_bulletin", ok=True, elapsed_ms=120, note="count=3"),
        _SourceCheck(
            name="gdacs_tc", ok=False, elapsed_ms=30000,
            note="SourceError: timeout for https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH",
        ),
    ]
    text, code = format_report(results)
    assert code == 1
    assert "1/2 sources OK" in text
    assert "gdacs_tc" in text
    assert "FAIL" in text


def test_format_report_contains_every_source_name() -> None:
    format_report = _load_format_report()
    names = [
        "imd_coastal_bulletin", "imd_geoserver", "incois_wfs", "incois_osf",
        "incois_argo", "openmeteo", "gdacs_tc", "marine_regions_imbl",
    ]
    results = [
        _SourceCheck(name=n, ok=(i % 2 == 0), elapsed_ms=100 + i, note=f"note-{i}")
        for i, n in enumerate(names)
    ]
    text, code = format_report(results)
    for n in names:
        assert n in text
    assert code == 1
    assert "4/8 sources OK" in text


def test_format_report_empty_results_is_vacuously_ok() -> None:
    format_report = _load_format_report()
    text, code = format_report([])
    assert code == 0
    assert "0/0 sources OK" in text
