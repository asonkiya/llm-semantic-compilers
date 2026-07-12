"""Coverage-grounded test linkage — measured ``covered_by``, not inferred.

Static call-edge linkage says a test *statically references* a component;
coverage contexts say a test *actually executed its lines*. When per-test
contexts exist (pytest-cov ``--cov-context=test`` or coverage.py's
``dynamic_context = test_function``), the pipeline maps covered lines onto
component spans and unions the result with static linkage: coverage adds
tests reached through indirection (fixtures, integration paths); static
keeps tests the coverage run skipped.

Sources, in preference order:
* ``.coverage`` — coverage.py's SQLite store, read via stdlib sqlite3. The
  line data is a "numbits" blob (bit N set = line N covered); the format is
  internal to coverage.py but has been stable for years — decode failures
  degrade gracefully to no coverage linkage.
* ``coverage.json`` — ``coverage json --show-contexts`` output.

No new dependency; no coverage data → identical behavior to before.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# rel_path -> normalized test id -> covered line numbers
CoverageData = dict[str, dict[str, set[int]]]


def numbits_to_lines(blob: bytes) -> set[int]:
    """Decode coverage.py's numbits: bit N set means line N covered."""
    lines: set[int] = set()
    for byte_index, byte in enumerate(blob):
        for bit in range(8):
            if byte & (1 << bit):
                lines.add(byte_index * 8 + bit)
    return lines


def normalize_context(context: str) -> str | None:
    """A coverage context name as a CGIR component id, or None to skip.

    pytest-cov: ``tests/test_m.py::TestX::test_f|run`` → ``tests.test_m.TestX.test_f``
    dynamic_context=test_function: already dotted. The empty string is the
    global (non-test) context.
    """
    if not context:
        return None
    context = context.split("|", 1)[0].split(" (", 1)[0].strip()
    if "::" in context:
        path, _, rest = context.partition("::")
        module = path.removesuffix(".py").replace("/", ".").replace("\\", ".")
        return f"{module}.{rest.replace('::', '.')}"
    return context


def read_coverage_contexts(repo: Path) -> CoverageData | None:
    """Per-test line coverage from ``.coverage`` or ``coverage.json``."""
    dot = repo / ".coverage"
    if dot.exists():
        data = _read_sqlite(dot, repo)
        if data:
            return data
    js = repo / "coverage.json"
    if js.exists():
        return _read_json(js)
    return None


def _read_sqlite(path: Path, repo: Path) -> CoverageData | None:
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            files = dict(db.execute("SELECT id, path FROM file"))
            contexts = dict(db.execute("SELECT id, context FROM context"))
            rows = list(db.execute("SELECT file_id, context_id, numbits FROM line_bits"))
        finally:
            db.close()
    except sqlite3.Error:
        return None

    out: CoverageData = {}
    for file_id, context_id, numbits in rows:
        test_id = normalize_context(str(contexts.get(context_id, "")))
        if test_id is None:
            continue
        rel = _relativize(str(files.get(file_id, "")), repo)
        if rel is None:
            continue
        lines = numbits_to_lines(numbits)
        if lines:
            out.setdefault(rel, {}).setdefault(test_id, set()).update(lines)
    return out or None


def _read_json(path: Path) -> CoverageData | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    out: CoverageData = {}
    for file_path, file_data in (payload.get("files") or {}).items():
        contexts = file_data.get("contexts") or {}
        for line_str, ctx_names in contexts.items():
            try:
                line = int(line_str)
            except ValueError:
                continue
            for name in ctx_names:
                test_id = normalize_context(str(name))
                if test_id is not None:
                    out.setdefault(file_path, {}).setdefault(test_id, set()).add(line)
    return out or None


def _relativize(file_path: str, repo: Path) -> str | None:
    try:
        return str(Path(file_path).resolve().relative_to(repo.resolve()))
    except ValueError:
        return file_path if file_path and not Path(file_path).is_absolute() else None


def coverage_covered_by(
    cov: CoverageData, spans: list[tuple[str, str, int, int]]
) -> dict[str, set[str]]:
    """Map coverage data onto component spans.

    ``spans`` is ``(component_id, rel_path, start_line, end_line)``. A test
    covers a component when any of its covered lines falls inside the span.
    A test never covers itself (its own body's lines are its execution).
    """
    out: dict[str, set[str]] = {}
    for component_id, rel_path, start, end in spans:
        per_test = cov.get(rel_path)
        if not per_test:
            continue
        for test_id, lines in per_test.items():
            if test_id == component_id:
                continue
            if any(start <= line <= end for line in lines):
                out.setdefault(component_id, set()).add(test_id)
    return out


__all__ = [
    "CoverageData",
    "coverage_covered_by",
    "normalize_context",
    "numbits_to_lines",
    "read_coverage_contexts",
]
