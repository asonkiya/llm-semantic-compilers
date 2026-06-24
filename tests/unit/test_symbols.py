"""Tests for per-module symbol tables and cross-file import resolution.

Previously listed as planned debt in ``docs/status.md`` — added now to pin the
relative-import behaviour landing in Sprint 4. Each test builds a minimal
repo with ``tmp_path`` and checks that an imported name binds to the
expected target node.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from cgir.analyses.call_graph import build_call_graph
from cgir.analyses.symbols import build_symbol_tables, resolve
from cgir.ir.edges import EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.sources import TreeSitterSource


def _write(repo: Path, rel: str, body: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dedent(body).lstrip())


def _ingest(repo: Path) -> RepoGraph:
    graph = TreeSitterSource().ingest(repo)
    return graph


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path


# --- Local bindings ---------------------------------------------------------


def test_local_function_binds_in_its_module(repo: Path) -> None:
    _write(repo, "m.py", "def f():\n    pass\n")
    graph = _ingest(repo)
    tables = build_symbol_tables(graph)
    assert resolve(tables, "module:m", "f") == "func:m.f"


def test_local_class_binds_in_its_module(repo: Path) -> None:
    _write(repo, "m.py", "class C:\n    pass\n")
    graph = _ingest(repo)
    tables = build_symbol_tables(graph)
    assert resolve(tables, "module:m", "C") == "class:m.C"


# --- Absolute imports ------------------------------------------------------


def test_absolute_from_import_resolves(repo: Path) -> None:
    _write(repo, "mypkg/pricing.py", "def add_tax(price, rate):\n    return price\n")
    _write(repo, "mypkg/orchestrator.py", "from mypkg.pricing import add_tax\n")
    graph = _ingest(repo)
    tables = build_symbol_tables(graph)
    assert resolve(tables, "module:mypkg.orchestrator", "add_tax") == "func:mypkg.pricing.add_tax"


# --- Relative imports ------------------------------------------------------


def test_single_dot_relative_import_resolves(repo: Path) -> None:
    _write(repo, "mypkg/__init__.py", "")
    _write(repo, "mypkg/pricing.py", "def add_tax(price, rate):\n    return price\n")
    _write(repo, "mypkg/orchestrator.py", "from .pricing import add_tax\n")
    graph = _ingest(repo)
    tables = build_symbol_tables(graph)
    assert resolve(tables, "module:mypkg.orchestrator", "add_tax") == "func:mypkg.pricing.add_tax"


def test_double_dot_relative_import_resolves(repo: Path) -> None:
    _write(repo, "mypkg/__init__.py", "")
    _write(repo, "mypkg/util.py", "def helper():\n    pass\n")
    _write(repo, "mypkg/sub/__init__.py", "")
    _write(repo, "mypkg/sub/inner.py", "from ..util import helper\n")
    graph = _ingest(repo)
    tables = build_symbol_tables(graph)
    assert resolve(tables, "module:mypkg.sub.inner", "helper") == "func:mypkg.util.helper"


def test_relative_import_drives_call_graph(repo: Path) -> None:
    """The relative-import target must also flow through to CALLS resolution."""
    _write(repo, "mypkg/__init__.py", "")
    _write(repo, "mypkg/pricing.py", "def add_tax(price, rate):\n    return price\n")
    _write(
        repo,
        "mypkg/orchestrator.py",
        """
        from .pricing import add_tax

        def quote(price):
            return add_tax(price, 0.08)
        """,
    )
    graph = _ingest(repo)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, repo)

    quote_id = "func:mypkg.orchestrator.quote"
    add_tax_id = "func:mypkg.pricing.add_tax"
    callees = {e.dst for e in graph.out_edges(quote_id, EdgeKind.CALLS)}
    assert add_tax_id in callees


def test_unresolved_external_import_stays_opaque(repo: Path) -> None:
    """A third-party / stdlib import we can't see binds to an opaque target."""
    _write(repo, "m.py", "from typing import List\n")
    graph = _ingest(repo)
    tables = build_symbol_tables(graph)
    # The name is recorded but its target is None (no in-graph node).
    table = tables["module:m"]
    assert "List" in table.bindings
    assert table.bindings["List"] is None


# --- IMPORTS edges (sanity) ------------------------------------------------


def test_imports_edge_exists_for_each_imported_name(repo: Path) -> None:
    _write(repo, "mypkg/__init__.py", "")
    _write(repo, "mypkg/util.py", "def helper():\n    pass\n")
    _write(repo, "mypkg/main.py", "from .util import helper\n")
    graph = _ingest(repo)
    edges = list(graph.out_edges("module:mypkg.main", EdgeKind.IMPORTS))
    assert len(edges) == 1
    # The IMPORTS edge target string should be the absolute name.
    assert edges[0].attrs.get("target") == "mypkg.util.helper"
