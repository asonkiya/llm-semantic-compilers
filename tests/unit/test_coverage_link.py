"""Coverage-grounded test linkage — measured `covered_by`, not inferred.

When per-test coverage contexts exist (pytest-cov ``--cov-context=test`` or
coverage's ``dynamic_context = test_function``), map covered line ranges
onto component spans: ground truth for "which tests exercise this
component," unioned with the static call-edge linkage (coverage adds tests
reached through indirection; static keeps tests the coverage run skipped).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cgir.analyses.coverage_link import (
    coverage_covered_by,
    normalize_context,
    numbits_to_lines,
    read_coverage_contexts,
)
from cgir.pipeline import scan_repo


def test_numbits_decode() -> None:
    # bit N set => line N covered: 0b0000_0110 -> lines 1,2; second byte bit 0 -> line 8
    assert numbits_to_lines(bytes([0b0000_0110, 0b0000_0001])) == {1, 2, 8}


def test_normalize_context_pytest_cov_style() -> None:
    assert normalize_context("tests/unit/test_m.py::test_f|run") == "tests.unit.test_m.test_f"
    assert normalize_context("tests/test_m.py::TestX::test_f|setup") == "tests.test_m.TestX.test_f"


def test_normalize_context_dotted_and_empty() -> None:
    assert normalize_context("tests.test_m.test_f") == "tests.test_m.test_f"
    assert normalize_context("") is None  # the global (non-test) context


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "pricing.py").write_text(
        "def add_tax(price, rate):\n    return price * (1 + rate)\n\n\n"
        "def discount(price):\n    return price * 0.9\n"
    )
    (tmp_path / "test_pricing.py").write_text(
        "from pricing import add_tax\n\n\ndef test_add_tax():\n    assert add_tax(100, 0.5) == 150\n"
    )
    return tmp_path


def _write_coverage_json(repo: Path) -> None:
    # coverage json --show-contexts format: files -> contexts -> line -> [ctx]
    payload = {
        "files": {
            "pricing.py": {
                "contexts": {
                    "2": ["test_pricing.py::test_add_tax|run"],
                }
            }
        }
    }
    (repo / "coverage.json").write_text(json.dumps(payload))


def test_read_coverage_json_contexts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write_coverage_json(repo)
    data = read_coverage_contexts(repo)
    assert data is not None
    assert data["pricing.py"]["test_pricing.test_add_tax"] == {2}


def test_read_dot_coverage_sqlite(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    db = sqlite3.connect(repo / ".coverage")
    db.executescript(
        """
        CREATE TABLE coverage_schema (version integer);
        INSERT INTO coverage_schema VALUES (7);
        CREATE TABLE file (id integer primary key, path text);
        CREATE TABLE context (id integer primary key, context text);
        CREATE TABLE line_bits (file_id integer, context_id integer, numbits blob);
        """
    )
    db.execute("INSERT INTO file VALUES (1, ?)", (str(repo / "pricing.py"),))
    db.execute("INSERT INTO context VALUES (1, '')")
    db.execute("INSERT INTO context VALUES (2, 'test_pricing.py::test_add_tax|run')")
    db.execute("INSERT INTO line_bits VALUES (1, 2, ?)", (bytes([0b0000_0100]),))  # line 2
    db.commit()
    db.close()
    data = read_coverage_contexts(repo)
    assert data is not None
    assert data["pricing.py"]["test_pricing.test_add_tax"] == {2}


def test_covered_by_maps_lines_to_component_spans(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write_coverage_json(repo)
    result = scan_repo(repo, out=tmp_path / ".cgir")
    by_id = {s.id: s for s in result.specs}
    # line 2 is inside add_tax's span -> covered; discount untouched
    assert "test_pricing.test_add_tax" in by_id["pricing.add_tax"].covered_by
    assert by_id["pricing.discount"].covered_by == []


def test_union_with_static_linkage(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write_coverage_json(repo)
    result = scan_repo(repo, out=tmp_path / ".cgir")
    by_id = {s.id: s for s in result.specs}
    # static call-edge linkage also finds test_add_tax (it calls add_tax);
    # coverage confirms it. Both sources union without duplicates.
    assert by_id["pricing.add_tax"].covered_by.count("test_pricing.test_add_tax") == 1


def test_no_coverage_data_changes_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = scan_repo(repo, out=tmp_path / ".cgir")
    by_id = {s.id: s for s in result.specs}
    # static linkage alone still works
    assert "test_pricing.test_add_tax" in by_id["pricing.add_tax"].covered_by


def test_coverage_covered_by_span_mapping() -> None:
    cov = {"pricing.py": {"tests.test_x.test_y": {2}, "tests.test_x.test_z": {99}}}
    spans = [("pricing.add_tax", "pricing.py", 1, 2), ("pricing.discount", "pricing.py", 5, 6)]
    out = coverage_covered_by(cov, spans)
    assert out == {"pricing.add_tax": {"tests.test_x.test_y"}}  # line 99 maps nowhere
