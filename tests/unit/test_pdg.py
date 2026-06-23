"""RED-phase tests for the program dependence graph (PDG) pass.

Contract:

* ``build(graph: RepoGraph) -> None`` — pure-graph (no ``repo_path``).
  Mutates ``graph`` in place by adding ``FLOWS_TO`` (data dependence) and
  ``DEPENDS_ON`` (control dependence) edges.
* ``FLOWS_TO``: from each ``Assignment`` / ``Parameter`` D to each CFG
  node N that *reads* (per ``attrs["reads"]``) one of the variables D
  writes, **iff** D reaches N (per reaching-defs).
* ``DEPENDS_ON``: from each CFG node N to the Branch/Loop id stored on
  ``N.attrs["controlled_by"]``. Top-level nodes have no controller.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from cgir.analyses.call_graph import build_call_graph
from cgir.analyses.cfg import build as build_cfg
from cgir.analyses.pdg import build as build_pdg
from cgir.analyses.symbols import build_symbol_tables
from cgir.ir.edges import EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import Node, NodeKind
from cgir.sources import TreeSitterSource


def _write(repo: Path, rel: str, body: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dedent(body).lstrip())


def _ingest(repo: Path) -> RepoGraph:
    graph = TreeSitterSource().ingest(repo)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, repo)
    build_cfg(graph, repo)
    build_pdg(graph)
    return graph


def _kind(g: RepoGraph, func_id: str, kind: NodeKind) -> list[Node]:
    return sorted(
        (c for c in g.children(func_id) if c.kind == kind),
        key=lambda n: (n.start_line or 0, n.id),
    )


def _flows_to(g: RepoGraph, src_id: str) -> set[str]:
    return {e.dst for e in g.out_edges(src_id, EdgeKind.FLOWS_TO)}


def _depends_on(g: RepoGraph, src_id: str) -> set[str]:
    return {e.dst for e in g.out_edges(src_id, EdgeKind.DEPENDS_ON)}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path


# --- Public API -------------------------------------------------------------


def test_signature_takes_only_graph(repo: Path) -> None:
    """``build(graph)`` is a pure-graph pass; no ``repo_path``."""
    _write(repo, "m.py", "def f():\n    pass\n")
    # If the signature requires repo_path, _ingest() will TypeError here.
    _ingest(repo)


# --- FLOWS_TO (data dependence) --------------------------------------------


def test_simple_data_dep(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f():
            x = 1
            return x
        """,
    )
    g = _ingest(repo)
    func_id = "func:m.f"
    [x_def] = _kind(g, func_id, NodeKind.Assignment)
    [ret] = _kind(g, func_id, NodeKind.Return)
    assert ret.id in _flows_to(g, x_def.id)


def test_reassignment_severs_data_dep(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f():
            x = 1
            x = 2
            return x
        """,
    )
    g = _ingest(repo)
    func_id = "func:m.f"
    x1, x2 = _kind(g, func_id, NodeKind.Assignment)
    [ret] = _kind(g, func_id, NodeKind.Return)
    assert ret.id not in _flows_to(g, x1.id)
    assert ret.id in _flows_to(g, x2.id)


def test_parameter_data_dep(repo: Path) -> None:
    _write(repo, "m.py", "def f(x):\n    return x\n")
    g = _ingest(repo)
    func_id = "func:m.f"
    [param] = _kind(g, func_id, NodeKind.Parameter)
    [ret] = _kind(g, func_id, NodeKind.Return)
    assert ret.id in _flows_to(g, param.id)


def test_branch_merge_unions_data_deps(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f(c):
            if c:
                x = 1
            else:
                x = 2
            return x
        """,
    )
    g = _ingest(repo)
    func_id = "func:m.f"
    assigns = _kind(g, func_id, NodeKind.Assignment)
    [ret] = _kind(g, func_id, NodeKind.Return)
    for a in assigns:
        assert ret.id in _flows_to(g, a.id)


def test_unread_def_has_no_flows_to(repo: Path) -> None:
    """A def whose value is never used produces no FLOWS_TO."""
    _write(
        repo,
        "m.py",
        """
        def f():
            x = 1
            y = 2
            return y
        """,
    )
    g = _ingest(repo)
    func_id = "func:m.f"
    assigns = _kind(g, func_id, NodeKind.Assignment)
    x_def = next(a for a in assigns if "x" in (a.attrs.get("writes") or []))
    assert _flows_to(g, x_def.id) == set()


def test_flows_to_only_to_nodes_that_read_the_var(repo: Path) -> None:
    """`return y` doesn't read x, so x_def should not FLOWS_TO it."""
    _write(
        repo,
        "m.py",
        """
        def f():
            x = 1
            y = 2
            return y
        """,
    )
    g = _ingest(repo)
    func_id = "func:m.f"
    assigns = _kind(g, func_id, NodeKind.Assignment)
    [ret] = _kind(g, func_id, NodeKind.Return)
    x_def = next(a for a in assigns if "x" in (a.attrs.get("writes") or []))
    y_def = next(a for a in assigns if "y" in (a.attrs.get("writes") or []))
    assert ret.id not in _flows_to(g, x_def.id)
    assert ret.id in _flows_to(g, y_def.id)


# --- DEPENDS_ON (control dependence) ---------------------------------------


def test_control_dep_if_body(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f(c):
            if c:
                x = 1
            return x
        """,
    )
    g = _ingest(repo)
    func_id = "func:m.f"
    [branch] = _kind(g, func_id, NodeKind.Branch)
    [assign] = _kind(g, func_id, NodeKind.Assignment)
    assert branch.id in _depends_on(g, assign.id)


def test_control_dep_loop_body(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f(items):
            for i in items:
                use(i)
        """,
    )
    g = _ingest(repo)
    func_id = "func:m.f"
    [loop] = _kind(g, func_id, NodeKind.Loop)
    stmts = _kind(g, func_id, NodeKind.Statement)
    assert loop.id in _depends_on(g, stmts[0].id)


def test_top_level_stmt_has_no_control_dep(repo: Path) -> None:
    _write(repo, "m.py", "def f():\n    x = 1\n    return x\n")
    g = _ingest(repo)
    func_id = "func:m.f"
    for child in g.children(func_id):
        if child.kind in {NodeKind.Assignment, NodeKind.Return}:
            assert _depends_on(g, child.id) == set()
