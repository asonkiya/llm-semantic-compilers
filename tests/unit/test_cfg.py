"""RED-phase tests for the intra-procedural CFG pass.

Contract under test:

* ``build(graph, repo_path) -> None`` mutates ``graph`` in place.
* For every ``Function`` / ``Method`` node, the pass emits CFG nodes
  (``Statement``, ``Assignment``, ``Branch``, ``Loop``, ``Return``) as
  ``CONTAINS`` children of the owning function, connected by
  ``CONTROLS`` edges.
* The function node is the CFG entry: it has at least one outgoing
  ``CONTROLS`` edge to the first body node.
* ``Return`` nodes are CFG sinks — they have no outgoing ``CONTROLS``.
* Branches without ``else`` fall through (the Branch node itself
  joins the post-branch successor set).
* Loops emit a back-edge: the last node of the loop body has a
  ``CONTROLS`` edge back to the loop header.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from cgir.analyses.call_graph import build_call_graph
from cgir.analyses.cfg import build as build_cfg
from cgir.analyses.symbols import build_symbol_tables
from cgir.ir.edges import EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import NodeKind
from cgir.sources import TreeSitterSource

CFG_KINDS = {
    NodeKind.Statement,
    NodeKind.Assignment,
    NodeKind.Branch,
    NodeKind.Loop,
    NodeKind.Return,
}


def _write(repo: Path, rel: str, body: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dedent(body).lstrip())


def _ingest_with_cfg(repo: Path) -> RepoGraph:
    graph = TreeSitterSource().ingest(repo)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, repo)
    build_cfg(graph, repo)
    return graph


def _cfg_children(graph: RepoGraph, func_id: str) -> list:
    return [child for child in graph.children(func_id) if child.kind in CFG_KINDS]


def _controls_chain(graph: RepoGraph, start_id: str) -> list[str]:
    """Walk a single deterministic CONTROLS successor chain from ``start_id``.

    Used in linear-flow tests where each node has exactly one successor.
    """
    visited: list[str] = []
    current = start_id
    seen: set[str] = set()
    while True:
        successors = list(graph.out_edges(current, EdgeKind.CONTROLS))
        if not successors or successors[0].dst in seen:
            return visited
        nxt = successors[0].dst
        visited.append(nxt)
        seen.add(nxt)
        current = nxt


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path


def test_linear_function_chains_three_nodes(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f():
            x = 1
            y = 2
            return x + y
        """,
    )
    g = _ingest_with_cfg(repo)
    func_id = "func:m.f"

    chain = _controls_chain(g, func_id)
    kinds = [g.get_node(n).kind for n in chain]
    assert kinds == [NodeKind.Assignment, NodeKind.Assignment, NodeKind.Return]


def test_return_is_a_sink(repo: Path) -> None:
    _write(repo, "m.py", "def f():\n    return 1\n")
    g = _ingest_with_cfg(repo)
    func_id = "func:m.f"

    chain = _controls_chain(g, func_id)
    assert len(chain) == 1
    return_id = chain[0]
    assert g.get_node(return_id).kind == NodeKind.Return
    assert list(g.out_edges(return_id, EdgeKind.CONTROLS)) == []


def test_function_contains_all_cfg_children(repo: Path) -> None:
    _write(repo, "m.py", "def f():\n    x = 1\n    return x\n")
    g = _ingest_with_cfg(repo)
    func_id = "func:m.f"

    children = _cfg_children(g, func_id)
    kinds = {c.kind for c in children}
    assert kinds == {NodeKind.Assignment, NodeKind.Return}


def test_if_else_creates_branch_with_two_successors(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f(x):
            if x:
                a = 1
            else:
                a = 2
            return a
        """,
    )
    g = _ingest_with_cfg(repo)
    func_id = "func:m.f"

    entries = list(g.out_edges(func_id, EdgeKind.CONTROLS))
    assert len(entries) == 1
    branch_id = entries[0].dst
    assert g.get_node(branch_id).kind == NodeKind.Branch

    branch_succs = [e.dst for e in g.out_edges(branch_id, EdgeKind.CONTROLS)]
    assert len(branch_succs) == 2
    succ_kinds = {g.get_node(s).kind for s in branch_succs}
    assert succ_kinds == {NodeKind.Assignment}


def test_if_without_else_falls_through(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f(x):
            if x:
                y = 1
            return x
        """,
    )
    g = _ingest_with_cfg(repo)
    func_id = "func:m.f"

    entry = next(iter(g.out_edges(func_id, EdgeKind.CONTROLS)))
    branch_id = entry.dst
    assert g.get_node(branch_id).kind == NodeKind.Branch

    succs = [g.get_node(e.dst) for e in g.out_edges(branch_id, EdgeKind.CONTROLS)]
    succ_kinds = {s.kind for s in succs}
    # One path goes into the then-body (Assignment), the other falls through to Return.
    assert succ_kinds == {NodeKind.Assignment, NodeKind.Return}


def test_if_elif_else_chain_has_two_branches(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f(x):
            if x == 1:
                a = 1
            elif x == 2:
                a = 2
            else:
                a = 3
            return a
        """,
    )
    g = _ingest_with_cfg(repo)
    func_id = "func:m.f"
    branch_count = sum(1 for c in _cfg_children(g, func_id) if c.kind == NodeKind.Branch)
    # One Branch for `if`, one for the elif condition.
    assert branch_count == 2


def test_for_loop_emits_back_edge(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f(items):
            for i in items:
                use(i)
            return None
        """,
    )
    g = _ingest_with_cfg(repo)
    func_id = "func:m.f"

    entry = next(iter(g.out_edges(func_id, EdgeKind.CONTROLS)))
    loop_id = entry.dst
    assert g.get_node(loop_id).kind == NodeKind.Loop

    # Body's tail must connect back to the loop header.
    back_edges = [e for e in g.in_edges(loop_id, EdgeKind.CONTROLS) if e.src != func_id]
    assert back_edges, "loop must have a back-edge from its body"

    # Loop has two outgoing CONTROLS: into the body and the fall-through exit.
    successors = [e.dst for e in g.out_edges(loop_id, EdgeKind.CONTROLS)]
    assert len(successors) == 2


def test_while_loop_emits_back_edge(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f(x):
            while x > 0:
                x = x - 1
            return x
        """,
    )
    g = _ingest_with_cfg(repo)
    func_id = "func:m.f"

    entry = next(iter(g.out_edges(func_id, EdgeKind.CONTROLS)))
    loop_id = entry.dst
    assert g.get_node(loop_id).kind == NodeKind.Loop
    back_edges = [e for e in g.in_edges(loop_id, EdgeKind.CONTROLS) if e.src != func_id]
    assert back_edges


def test_nested_if_inside_loop(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f(items):
            for i in items:
                if i:
                    use(i)
        """,
    )
    g = _ingest_with_cfg(repo)
    func_id = "func:m.f"
    children = _cfg_children(g, func_id)
    kinds = [c.kind for c in children]
    assert NodeKind.Loop in kinds
    assert NodeKind.Branch in kinds


def test_cfg_nodes_carry_source_locations(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f():
            x = 1
            return x
        """,
    )
    g = _ingest_with_cfg(repo)
    func_id = "func:m.f"
    for child in _cfg_children(g, func_id):
        assert child.path == "m.py"
        assert child.start_line is not None and child.start_line >= 1


def test_cfg_does_not_break_existing_passes(python_sample_repo: Path) -> None:
    """The fixture pipeline still produces a pure_function for add_tax."""
    from cgir.analyses.effects import classify
    from cgir.analyses.purity import score
    from cgir.ir.component_spec import ComponentKind
    from cgir.slicing import slice_components

    g = _ingest_with_cfg(python_sample_repo)
    effects = classify(g, python_sample_repo)
    purity = score(g, effects)
    specs = {s.id: s for s in slice_components(g, effects=effects, purity_scores=purity)}
    assert specs["pricing.add_tax"].kind == ComponentKind.pure_function
    assert specs["pricing.add_tax"].purity == 1.0


# --- Assignment "writes" attr (consumed by reaching_defs) -------------------


def _assignments(graph: RepoGraph, func_id: str) -> list:
    return sorted(
        (c for c in graph.children(func_id) if c.kind == NodeKind.Assignment),
        key=lambda n: n.start_line or 0,
    )


def test_simple_assignment_records_single_write(repo: Path) -> None:
    _write(repo, "m.py", "def f():\n    x = 1\n")
    g = _ingest_with_cfg(repo)
    [assign] = _assignments(g, "func:m.f")
    assert assign.attrs.get("writes") == ["x"]


def test_tuple_assignment_records_all_writes(repo: Path) -> None:
    _write(repo, "m.py", "def f():\n    a, b = 1, 2\n")
    g = _ingest_with_cfg(repo)
    [assign] = _assignments(g, "func:m.f")
    assert set(assign.attrs.get("writes") or []) == {"a", "b"}


def test_subscript_assignment_records_no_writes(repo: Path) -> None:
    """`xs[0] = 1` mutates `xs`'s contents but does not (re)define the name."""
    _write(repo, "m.py", "def f(xs):\n    xs[0] = 1\n")
    g = _ingest_with_cfg(repo)
    [assign] = _assignments(g, "func:m.f")
    assert assign.attrs.get("writes") == []


def test_attribute_assignment_records_no_writes(repo: Path) -> None:
    _write(repo, "m.py", "def f(obj):\n    obj.x = 1\n")
    g = _ingest_with_cfg(repo)
    [assign] = _assignments(g, "func:m.f")
    assert assign.attrs.get("writes") == []


# --- CFG node reads (feed PDG data dep) -------------------------------------


def test_assignment_records_rhs_reads(repo: Path) -> None:
    _write(repo, "m.py", "def f(y, z):\n    x = y + z\n")
    g = _ingest_with_cfg(repo)
    [assign] = _assignments(g, "func:m.f")
    assert set(assign.attrs.get("reads") or []) == {"y", "z"}


def test_return_records_expression_reads(repo: Path) -> None:
    _write(repo, "m.py", "def f(x):\n    return x\n")
    g = _ingest_with_cfg(repo)
    [ret] = (c for c in g.children("func:m.f") if c.kind == NodeKind.Return)
    assert ret.attrs.get("reads") == ["x"]


def test_branch_records_condition_reads(repo: Path) -> None:
    _write(repo, "m.py", "def f(c):\n    if c:\n        x = 1\n")
    g = _ingest_with_cfg(repo)
    branches = [c for c in g.children("func:m.f") if c.kind == NodeKind.Branch]
    assert branches[0].attrs.get("reads") == ["c"]


def test_for_loop_records_iterable_reads(repo: Path) -> None:
    _write(repo, "m.py", "def f(items):\n    for i in items:\n        use(i)\n")
    g = _ingest_with_cfg(repo)
    loops = [c for c in g.children("func:m.f") if c.kind == NodeKind.Loop]
    assert loops[0].attrs.get("reads") == ["items"]


def test_while_loop_records_condition_reads(repo: Path) -> None:
    _write(repo, "m.py", "def f(x):\n    while x > 0:\n        x = x - 1\n")
    g = _ingest_with_cfg(repo)
    loops = [c for c in g.children("func:m.f") if c.kind == NodeKind.Loop]
    assert loops[0].attrs.get("reads") == ["x"]


def test_reads_excludes_attribute_names(repo: Path) -> None:
    """For ``obj.x``, only ``obj`` is a read; ``x`` is an attribute name."""
    _write(repo, "m.py", "def f(obj):\n    y = obj.x\n")
    g = _ingest_with_cfg(repo)
    [assign] = _assignments(g, "func:m.f")
    assert assign.attrs.get("reads") == ["obj"]


def test_reads_excludes_called_function_name(repo: Path) -> None:
    """``add_tax(price, rate)``: reads are the args, not the callee name."""
    _write(repo, "m.py", "def f(price, rate):\n    result = add_tax(price, rate)\n")
    g = _ingest_with_cfg(repo)
    [assign] = _assignments(g, "func:m.f")
    assert set(assign.attrs.get("reads") or []) == {"price", "rate"}


# --- mutates (attribute / subscript LHS) ------------------------------------


def test_attribute_assignment_records_mutates_not_writes(repo: Path) -> None:
    _write(repo, "m.py", "def f(obj):\n    obj.x = 1\n")
    g = _ingest_with_cfg(repo)
    [assign] = _assignments(g, "func:m.f")
    assert assign.attrs.get("writes") == []
    assert assign.attrs.get("mutates") == ["obj"]


def test_subscript_assignment_records_mutates_not_writes(repo: Path) -> None:
    _write(repo, "m.py", "def f(xs):\n    xs[0] = 1\n")
    g = _ingest_with_cfg(repo)
    [assign] = _assignments(g, "func:m.f")
    assert assign.attrs.get("writes") == []
    assert assign.attrs.get("mutates") == ["xs"]


def test_simple_assignment_records_no_mutates(repo: Path) -> None:
    _write(repo, "m.py", "def f():\n    x = 1\n")
    g = _ingest_with_cfg(repo)
    [assign] = _assignments(g, "func:m.f")
    assert assign.attrs.get("mutates") == []


# --- controlled_by (drives PDG control dep) ---------------------------------


def test_top_level_stmt_has_no_controller(repo: Path) -> None:
    _write(repo, "m.py", "def f():\n    x = 1\n    return x\n")
    g = _ingest_with_cfg(repo)
    for c in _cfg_children(g, "func:m.f"):
        assert c.attrs.get("controlled_by") is None


def test_if_body_stmt_controlled_by_branch(repo: Path) -> None:
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
    g = _ingest_with_cfg(repo)
    func_id = "func:m.f"
    branches = [c for c in g.children(func_id) if c.kind == NodeKind.Branch]
    assigns = [c for c in g.children(func_id) if c.kind == NodeKind.Assignment]
    assert assigns[0].attrs.get("controlled_by") == branches[0].id


def test_else_body_stmts_share_branch_as_controller(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f(c):
            if c:
                x = 1
            else:
                x = 2
        """,
    )
    g = _ingest_with_cfg(repo)
    func_id = "func:m.f"
    branches = [c for c in g.children(func_id) if c.kind == NodeKind.Branch]
    assigns = [c for c in g.children(func_id) if c.kind == NodeKind.Assignment]
    assert {a.attrs.get("controlled_by") for a in assigns} == {branches[0].id}


def test_loop_body_stmt_controlled_by_loop(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f(items):
            for i in items:
                use(i)
        """,
    )
    g = _ingest_with_cfg(repo)
    func_id = "func:m.f"
    loops = [c for c in g.children(func_id) if c.kind == NodeKind.Loop]
    stmts = [c for c in g.children(func_id) if c.kind == NodeKind.Statement]
    assert stmts[0].attrs.get("controlled_by") == loops[0].id


def test_nested_branch_uses_inner_branch_as_controller(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def f(a, b):
            if a:
                if b:
                    x = 1
        """,
    )
    g = _ingest_with_cfg(repo)
    func_id = "func:m.f"
    branches = sorted(
        (c for c in g.children(func_id) if c.kind == NodeKind.Branch),
        key=lambda n: n.start_line or 0,
    )
    outer, inner = branches
    [assign] = (c for c in g.children(func_id) if c.kind == NodeKind.Assignment)
    assert assign.attrs.get("controlled_by") == inner.id
    assert inner.attrs.get("controlled_by") == outer.id
