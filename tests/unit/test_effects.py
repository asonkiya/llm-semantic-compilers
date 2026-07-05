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


# --- alias-aware matching (Sprint 11) -----------------------------------------


def test_aliased_module_import_is_net(repo: Path) -> None:
    """`import requests as r; r.get(url)` resolves through the import alias."""
    _write(
        repo,
        "m.py",
        """
        import requests as r

        def fetch(url):
            return r.get(url)
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert "net" in effects["func:m.fetch"]


def test_from_import_bare_call_is_fs(repo: Path) -> None:
    """`from os import remove; remove(p)` — a bare callee resolved via binding."""
    _write(
        repo,
        "m.py",
        """
        from os import remove

        def rm(p):
            remove(p)
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert "fs" in effects["func:m.rm"]


def test_from_import_bare_call_is_net(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        from urllib.request import urlopen

        def fetch(u):
            return urlopen(u)
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert "net" in effects["func:m.fetch"]


def test_aliased_bare_call_is_nondeterm(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        from uuid import uuid4 as fresh

        def make_id():
            return fresh()
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert "nondeterm" in effects["func:m.make_id"]


def test_urllib_parse_is_not_net(repo: Path) -> None:
    """urllib.parse is pure string manipulation — must not trip the net prefix."""
    _write(
        repo,
        "m.py",
        """
        import urllib.parse

        def join(base, href):
            return urllib.parse.urljoin(base, href)
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert effects["func:m.join"] == []


def test_unrelated_bare_call_stays_untagged(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        from mylib import helper

        def go(x):
            return helper(x)
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert effects["func:m.go"] == []


# --- db tag (settled Sprint 13) ------------------------------------------------


def test_db_query_is_db_effect(repo: Path) -> None:
    _write(repo, "m.py", "def find(db, i):\n    return db.query(i).first()\n")
    effects = classify(_ingest(repo), repo)
    assert "db" in effects["func:m.find"]


def test_session_commit_is_db_effect(repo: Path) -> None:
    _write(repo, "m.py", "def save(session):\n    session.commit()\n")
    effects = classify(_ingest(repo), repo)
    assert "db" in effects["func:m.save"]


def test_cursor_execute_is_db_effect(repo: Path) -> None:
    _write(repo, "m.py", "def run(cursor, sql):\n    cursor.execute(sql)\n")
    effects = classify(_ingest(repo), repo)
    assert "db" in effects["func:m.run"]


def test_nested_db_receiver_is_db_effect(repo: Path) -> None:
    """`self.db.execute(...)` — the receiver segment right before the method."""
    _write(repo, "m.py", "class R:\n    def run(self, sql):\n        self.db.execute(sql)\n")
    effects = classify(_ingest(repo), repo)
    assert "db" in effects["method:m.R.run"]


def test_non_db_receiver_is_not_tagged(repo: Path) -> None:
    """`config.get(...)` must not trip the db heuristic — receiver-gated."""
    _write(repo, "m.py", "def read(config, key):\n    return config.get(key)\n")
    effects = classify(_ingest(repo), repo)
    assert effects["func:m.read"] == []


# --- CV / ML media and model IO (found on camera-tracking) ---------------------


def test_cv2_videocapture_is_io(repo: Path) -> None:
    """Opening a camera device / RTSP stream is IO — found classified pure."""
    _write(
        repo,
        "m.py",
        """
        import cv2

        def read_video(source):
            cap = cv2.VideoCapture(source)
            return cap
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert "io" in effects["func:m.read_video"]


def test_cv2_imread_imwrite_are_fs(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        import cv2

        def snapshot(path, frame):
            cv2.imwrite(path, frame)

        def load(path):
            return cv2.imread(path)
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert "fs" in effects["func:m.snapshot"]
    assert "fs" in effects["func:m.load"]


def test_cv2_pure_image_ops_stay_untagged(repo: Path) -> None:
    """resize/cvtColor are pure math — a blanket cv2. prefix would be wrong."""
    _write(
        repo,
        "m.py",
        """
        import cv2

        def shrink(frame):
            return cv2.resize(frame, (640, 360))
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert effects["func:m.shrink"] == []


def test_torch_load_save_are_fs(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        import torch

        def load_model(path):
            return torch.load(path)

        def save_model(model, path):
            torch.save(model, path)
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert "fs" in effects["func:m.load_model"]
    assert "fs" in effects["func:m.save_model"]


def test_np_random_is_nondeterm(repo: Path) -> None:
    _write(
        repo,
        "m.py",
        """
        import numpy as np

        def jitter(x):
            return x + np.random.rand()
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert "nondeterm" in effects["func:m.jitter"]


# --- raise is not impure (settled Sprint 13) -----------------------------------


def test_raise_only_callee_does_not_taint_caller(repo: Path) -> None:
    """Calling a raise-only validator must not mark the caller calls_effectful."""
    _write(repo, "checks.py", "def ensure(c):\n    if not c:\n        raise ValueError('no')\n")
    _write(
        repo,
        "caller.py",
        """
        from checks import ensure

        def use(x):
            ensure(x)
            return x
        """,
    )
    effects = classify(_ingest(repo), repo)
    assert "raise" in effects["func:checks.ensure"]
    assert "calls_effectful" not in effects["func:caller.use"]
