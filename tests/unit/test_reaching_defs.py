"""RED-phase tests for the reaching-definitions worklist analysis.

Contract under test:

* ``compute(graph: RepoGraph) -> dict[str, set[str]]`` — **no** ``repo_path``.
  This is the first pure-graph analysis: it reads ``Assignment.attrs["writes"]``
  populated by :mod:`cgir.analyses.cfg` and walks ``CONTROLS`` edges. It does
  not re-parse source. See ``docs/roadmap.md`` "Grammar-agnostic core refactor".
* Returns ``{cfg_node_id: {def_id, ...}}`` where ``def_id`` is either an
  ``Assignment`` node id or a ``Parameter`` node id (parameters count as
  definitions at function entry).
* Forward, may-analysis: an Assignment to ``v`` kills all *other* defs of
  ``v``; defs of different variables don't interfere.
* Loops carry defs around back-edges (fixed-point iteration).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from cgir.analyses.call_graph import build_call_graph
from cgir.analyses.cfg import build as build_cfg
from cgir.analyses.reaching_defs import compute
from cgir.analyses.symbols import build_symbol_tables
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
    return graph


def _children_of_kind(g: RepoGraph, func_id: str, kind: NodeKind) -> list[Node]:
    return sorted(
        (c for c in g.children(func_id) if c.kind == kind),
        key=lambda n: (n.start_line or 0, n.id),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path


def test_signature_takes_only_graph(repo: Path) -> None:
    """The pass is pure-graph: no ``repo_path`` parameter."""
    _write(repo, "m.py", "def f():\n    x = 1\n    return x\n")
    g = _ingest(repo)
    rd = compute(g)
    assert isinstance(rd, dict)


def test_simple_linear_def_reaches_use(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f():
            x = 1
            y = x
            return y
        """,
    )
    g = _ingest(repo)
    rd = compute(g)
    func_id = "func:m.f"

    x_def, y_def = _children_of_kind(g, func_id, NodeKind.Assignment)
    [ret] = _children_of_kind(g, func_id, NodeKind.Return)

    assert x_def.id in rd[y_def.id]
    assert x_def.id in rd[ret.id]
    assert y_def.id in rd[ret.id]


def test_reassignment_kills_prior_def(repo: Path) -> None:
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
    rd = compute(g)
    func_id = "func:m.f"

    x1, x2 = _children_of_kind(g, func_id, NodeKind.Assignment)
    [ret] = _children_of_kind(g, func_id, NodeKind.Return)

    assert x1.id not in rd[ret.id], "first def must be killed by the second"
    assert x2.id in rd[ret.id]


def test_branch_merge_unions_both_defs(repo: Path) -> None:
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
    rd = compute(g)
    func_id = "func:m.f"

    assigns = _children_of_kind(g, func_id, NodeKind.Assignment)
    [ret] = _children_of_kind(g, func_id, NodeKind.Return)
    assert {a.id for a in assigns} <= rd[ret.id]


def test_parameter_reaches_entry_use(repo: Path) -> None:
    _write(repo, "m.py", "def f(x):\n    return x\n")
    g = _ingest(repo)
    rd = compute(g)
    func_id = "func:m.f"

    [param] = _children_of_kind(g, func_id, NodeKind.Parameter)
    [ret] = _children_of_kind(g, func_id, NodeKind.Return)
    assert param.id in rd[ret.id]


def test_loop_carries_def_through_back_edge(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f():
            x = 0
            while x < 10:
                x = x + 1
            return x
        """,
    )
    g = _ingest(repo)
    rd = compute(g)
    func_id = "func:m.f"

    x_init, x_step = _children_of_kind(g, func_id, NodeKind.Assignment)
    # Inside the loop body, the prior iteration's `x = x + 1` must reach back
    # via the back-edge. The initial `x = 0` also reaches the first iteration.
    assert x_step.id in rd[x_step.id]
    assert x_init.id in rd[x_step.id]


def test_different_variables_dont_kill(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f():
            x = 1
            y = 2
            return x
        """,
    )
    g = _ingest(repo)
    rd = compute(g)
    func_id = "func:m.f"

    x_def, y_def = _children_of_kind(g, func_id, NodeKind.Assignment)
    [ret] = _children_of_kind(g, func_id, NodeKind.Return)
    assert x_def.id in rd[ret.id]
    assert y_def.id in rd[ret.id], "an unrelated y= must not kill x's def"


def test_function_with_no_defs_yields_empty_in_sets(repo: Path) -> None:
    _write(repo, "m.py", "def f():\n    pass\n")
    g = _ingest(repo)
    rd = compute(g)
    func_id = "func:m.f"

    cfg_kinds = {
        NodeKind.Statement,
        NodeKind.Assignment,
        NodeKind.Branch,
        NodeKind.Loop,
        NodeKind.Return,
    }
    for child in g.children(func_id):
        if child.kind in cfg_kinds:
            assert rd[child.id] == set()


def test_returns_entry_for_every_cfg_node(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f(x):
            if x:
                y = 1
            return y
        """,
    )
    g = _ingest(repo)
    rd = compute(g)
    func_id = "func:m.f"

    cfg_kinds = {
        NodeKind.Statement,
        NodeKind.Assignment,
        NodeKind.Branch,
        NodeKind.Loop,
        NodeKind.Return,
    }
    expected = {c.id for c in g.children(func_id) if c.kind in cfg_kinds}
    assert expected <= set(rd)


# --- generalized defs: any CFG node with a "writes" attr (Sprint 5) ----------


def test_with_alias_is_a_def(repo: Path) -> None:
    """`with open(p) as fh:` defines fh; the header node reaches the body use."""
    _write(
        repo,
        "m.py",
        """
        def f(p):
            with open(p) as fh:
                data = fh.read()
        """,
    )
    g = _ingest(repo)
    rd = compute(g)
    func_id = "func:m.f"

    header = next(
        c
        for c in g.children(func_id)
        if c.kind == NodeKind.Statement and "fh" in (c.attrs.get("writes") or [])
    )
    [assign] = _children_of_kind(g, func_id, NodeKind.Assignment)
    assert header.id in rd[assign.id]


def test_for_target_is_a_def(repo: Path) -> None:
    """`for i in items:` defines i; the loop header reaches the body use."""
    _write(
        repo,
        "m.py",
        """
        def f(items):
            for i in items:
                y = i
        """,
    )
    g = _ingest(repo)
    rd = compute(g)
    func_id = "func:m.f"

    [loop] = _children_of_kind(g, func_id, NodeKind.Loop)
    [assign] = _children_of_kind(g, func_id, NodeKind.Assignment)
    assert loop.id in rd[assign.id]
