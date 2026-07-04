"""RED-phase tests for the purity scorer.

Contract:

* ``score(graph, effects) -> dict[str, float]`` returns one entry per
  Function/Method node.
* Score rubric (from ``Code-IR.md`` and ``cgir/analyses/purity.py``):
    1.0  no own impure effects AND only calls into pure components
    0.7  ``calls_effectful`` only (inherits effects via callees but does
         no direct IO/state writes itself)
    0.0  any direct impure effect ``io | net | fs | nondeterm | db``
* ``raise`` is *not* impure (settled Sprint 13): exceptions are control
  flow / part of the contract. A raise-only function scores 1.0 while the
  ``raise`` tag stays visible in its effects list.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from cgir.analyses.call_graph import build_call_graph
from cgir.analyses.effects import classify
from cgir.analyses.purity import score
from cgir.analyses.symbols import build_symbol_tables
from cgir.ir.graph import RepoGraph
from cgir.sources import TreeSitterSource


def _ingest(repo: Path) -> RepoGraph:
    graph = TreeSitterSource().ingest(repo)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, repo)
    return graph


def _write(repo: Path, rel: str, body: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(dedent(body).lstrip())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path


def test_pure_function_scores_one(repo: Path) -> None:
    _write(repo, "pricing.py", "def add_tax(price, rate):\n    return price * (1 + rate)\n")
    graph = _ingest(repo)
    scores = score(graph, classify(graph, repo))
    assert scores["func:pricing.add_tax"] == 1.0


def test_io_function_scores_zero(repo: Path) -> None:
    _write(repo, "writer.py", "def loud(x):\n    print(x)\n")
    graph = _ingest(repo)
    scores = score(graph, classify(graph, repo))
    assert scores["func:writer.loud"] == 0.0


def test_raise_only_function_stays_pure(repo: Path) -> None:
    """raise is control flow, not I/O — a raise-only validator scores 1.0."""
    _write(repo, "boom.py", "def boom():\n    raise ValueError('x')\n")
    graph = _ingest(repo)
    scores = score(graph, classify(graph, repo))
    assert scores["func:boom.boom"] == 1.0


def test_raise_plus_io_still_scores_zero(repo: Path) -> None:
    _write(repo, "boom.py", "def boom():\n    print('x')\n    raise ValueError('x')\n")
    graph = _ingest(repo)
    scores = score(graph, classify(graph, repo))
    assert scores["func:boom.boom"] == 0.0


def test_db_call_scores_zero(repo: Path) -> None:
    _write(repo, "repo.py", "def save(db, obj):\n    db.commit()\n")
    graph = _ingest(repo)
    scores = score(graph, classify(graph, repo))
    assert scores["func:repo.save"] == 0.0


def test_transitive_caller_of_effectful_scores_below_pure(repo: Path) -> None:
    _write(repo, "leaf.py", "def speak(x):\n    print(x)\n")
    _write(
        repo,
        "caller.py",
        """
        from leaf import speak

        def relay(x):
            speak(x)
        """,
    )
    graph = _ingest(repo)
    scores = score(graph, classify(graph, repo))
    assert scores["func:caller.relay"] == pytest.approx(0.7)


def test_caller_of_pure_function_is_pure(repo: Path) -> None:
    _write(repo, "pricing.py", "def add_tax(price, rate):\n    return price * (1 + rate)\n")
    _write(
        repo,
        "orchestrator.py",
        """
        from pricing import add_tax

        def quote(price):
            return add_tax(price, 0.08)
        """,
    )
    graph = _ingest(repo)
    scores = score(graph, classify(graph, repo))
    assert scores["func:orchestrator.quote"] == 1.0
