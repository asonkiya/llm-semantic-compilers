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


# --- extended taxonomy: net / fs / nondeterm (Sprint 5) -----------------------


def test_requests_call_is_net_effect(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        import requests

        def fetch(url):
            return requests.get(url)
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert "net" in effects["func:m.fetch"]


def test_urllib_call_is_net_effect(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        import urllib.request

        def fetch(url):
            return urllib.request.urlopen(url)
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert "net" in effects["func:m.fetch"]


def test_os_remove_is_fs_effect(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        import os

        def rm(p):
            os.remove(p)
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert "fs" in effects["func:m.rm"]


def test_shutil_rmtree_is_fs_effect(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        import shutil

        def wipe(d):
            shutil.rmtree(d)
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert "fs" in effects["func:m.wipe"]


def test_path_write_text_is_fs_effect(repo: Path) -> None:
    _write(repo, "m.py", "def save(p, s):\n    p.write_text(s)\n")
    effects = classify(_ingest(repo), repo)
    assert "fs" in effects["func:m.save"]


def test_random_call_is_nondeterm_effect(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        import random

        def roll():
            return random.randint(1, 6)
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert "nondeterm" in effects["func:m.roll"]


def test_time_time_is_nondeterm_effect(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        import time

        def stamp():
            return time.time()
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert "nondeterm" in effects["func:m.stamp"]


def test_datetime_now_is_nondeterm_effect(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        from datetime import datetime

        def stamp():
            return datetime.now()
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert "nondeterm" in effects["func:m.stamp"]


def test_uuid4_is_nondeterm_effect(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        import uuid

        def fresh_id():
            return uuid.uuid4()
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert "nondeterm" in effects["func:m.fresh_id"]


def test_pure_attribute_call_is_not_tagged(repo: Path) -> None:
    """An arbitrary method call must not trip the net/fs/nondeterm heuristics."""
    _write(repo, "m.py", "def up(s):\n    return s.upper()\n")
    effects = classify(_ingest(repo), repo)
    assert effects["func:m.up"] == []
