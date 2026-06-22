"""RED-phase tests for the effects classifier.

Contract under test:

* ``classify(graph, repo_path) -> dict[str, list[str]]`` returns a mapping
  from function/method node id to a sorted list of effect tags.
* Tags use the documented taxonomy: ``"io"``, ``"raise"``, plus the
  transitive-only synthetic tag ``"calls_effectful"`` so callers can tell
  whether a node's effect came from itself or a callee.
* Pure functions return ``[]`` (not absent from the dict).
* Effects are computed transitively over ``CALLS`` edges: if A calls B and
  B has ``io``, A's effect list includes ``calls_effectful``.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from cgir.analyses.call_graph import build_call_graph
from cgir.analyses.effects import classify
from cgir.analyses.symbols import build_symbol_tables
from cgir.ir.graph import RepoGraph
from cgir.sources import TreeSitterSource


def _ingest(repo: Path) -> RepoGraph:
    graph = TreeSitterSource().ingest(repo)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, repo)
    return graph


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path


def _write(repo: Path, rel: str, body: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dedent(body).lstrip())


def test_pure_arithmetic_has_no_effects(repo: Path) -> None:
    _write(repo, "pricing.py", "def add_tax(price, rate):\n    return price * (1 + rate)\n")
    graph = _ingest(repo)
    effects = classify(graph, repo)
    assert effects["func:pricing.add_tax"] == []


def test_print_call_is_io_effect(repo: Path) -> None:
    _write(repo, "writer.py", "def loud(x):\n    print(x)\n")
    graph = _ingest(repo)
    effects = classify(graph, repo)
    assert "io" in effects["func:writer.loud"]


def test_input_call_is_io_effect(repo: Path) -> None:
    _write(repo, "reader.py", "def ask():\n    return input('?')\n")
    graph = _ingest(repo)
    effects = classify(graph, repo)
    assert "io" in effects["func:reader.ask"]


def test_raise_statement_is_raise_effect(repo: Path) -> None:
    _write(repo, "boom.py", "def boom():\n    raise ValueError('x')\n")
    graph = _ingest(repo)
    effects = classify(graph, repo)
    assert "raise" in effects["func:boom.boom"]


def test_effects_propagate_transitively_through_calls(repo: Path) -> None:
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
    effects = classify(graph, repo)
    assert "io" in effects["func:leaf.speak"]
    assert "calls_effectful" in effects["func:caller.relay"]


def test_classify_returns_entry_for_every_function(repo: Path) -> None:
    _write(repo, "m.py", "def a():\n    return 1\n\ndef b():\n    print(1)\n")
    graph = _ingest(repo)
    effects = classify(graph, repo)
    assert set(effects) == {"func:m.a", "func:m.b"}
