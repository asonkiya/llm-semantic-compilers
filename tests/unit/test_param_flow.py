"""RED-phase tests for parameter-flow analysis (Sprint 15).

Contract:

* ``compute(graph) -> dict[str, list[dict]]`` — pure-graph (no repo_path).
  Per caller function id: a list of ``{"callee": id, "params": [...]}``
  entries naming which of the *caller's parameters* (may-)flow into the
  arguments of that call.
* Flow is name-taint over the CFG ``reads``/``writes`` attrs populated by
  :mod:`cgir.analyses.cfg` — transitive through local assignments,
  order-insensitive (a may-analysis; flag, don't prove).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from cgir.analyses.call_graph import build_call_graph
from cgir.analyses.cfg import build as build_cfg
from cgir.analyses.param_flow import compute
from cgir.analyses.symbols import build_symbol_tables
from cgir.ir.graph import RepoGraph
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


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path


def _flows_for(graph: RepoGraph, caller: str, callee: str) -> list[str]:
    for entry in compute(graph).get(caller, []):
        if entry["callee"] == callee:
            return list(entry["params"])
    return []


def test_direct_param_forwarding(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def save(db, obj):
            db.add(obj)

        def create(db, payload):
            save(db, payload)
        """,
    )
    g = _ingest(repo)
    assert _flows_for(g, "func:m.create", "func:m.save") == ["db", "payload"]


def test_transitive_flow_through_local(repo: Path) -> None:
    """x -> y via assignment, then y into the call: x still flows."""
    _write(
        repo,
        "m.py",
        """
        def use(v):
            return v

        def relay(x):
            y = x + 1
            return use(y)
        """,
    )
    g = _ingest(repo)
    assert _flows_for(g, "func:m.relay", "func:m.use") == ["x"]


def test_unrelated_param_does_not_flow(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def use(v):
            return v

        def pick(a, b):
            return use(a)
        """,
    )
    g = _ingest(repo)
    assert _flows_for(g, "func:m.pick", "func:m.use") == ["a"]


def test_constant_args_flow_nothing(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        def use(v):
            return v

        def fixed(x):
            return use(42)
        """,
    )
    g = _ingest(repo)
    assert _flows_for(g, "func:m.fixed", "func:m.use") == []


def test_pure_graph_signature(repo: Path) -> None:
    """compute(graph) takes only the graph — no repo_path re-parse."""
    _write(repo, "m.py", "def f():\n    pass\n")
    result = compute(_ingest(repo))
    assert isinstance(result, dict)
