"""Rung 5: capture real I/O from a test run, replay it against candidates.

Includes a function whose input is a *dataclass* — the case random-input
synthesis handled poorly — to show capture covers what synthesis can't.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cgir.replay import capture, make_replay_oracle, replay

MODULE = """\
from dataclasses import dataclass


@dataclass
class Box:
    w: int
    h: int


def area(b: Box) -> int:
    return b.w * b.h


def clamp(x: int, lo: int, hi: int) -> int:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x
"""

TEST = """\
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from shapes import Box, area, clamp


def test_area():
    assert area(Box(3, 4)) == 12
    assert area(Box(0, 9)) == 0


def test_clamp():
    assert clamp(5, 0, 10) == 5
    assert clamp(-3, 0, 10) == 0
    assert clamp(99, 0, 10) == 10
"""


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "shapes.py").write_text(MODULE)
    (repo / "test_shapes.py").write_text(TEST)
    return repo


@pytest.fixture
def captured(tmp_path: Path):
    repo = _repo(tmp_path)
    traces = capture(
        repo,
        {"shapes.area": (Path("shapes.py"), "area"), "shapes.clamp": (Path("shapes.py"), "clamp")},
    )
    return repo, traces


def test_capture_records_real_io(captured) -> None:
    _, traces = captured
    assert len(traces["shapes.area"]) == 2  # two real calls in the test
    assert len(traces["shapes.clamp"]) == 3
    # the dataclass input was captured verbatim — no synthesis needed
    (box_arg,), result = traces["shapes.area"][0]
    assert (box_arg.w, box_arg.h) == (3, 4) and result == 12


def test_replay_accepts_correct_rewrite(captured) -> None:
    repo, traces = captured
    good = "def area(b):\n    return b.h * b.w\n"  # restructured, same behavior
    ok, feedback = replay(repo, "shapes.area", good, traces["shapes.area"])
    assert ok, feedback


def test_replay_rejects_wrong_rewrite(captured) -> None:
    repo, traces = captured
    wrong = "def area(b):\n    return b.w + b.h\n"  # + instead of *
    ok, feedback = replay(repo, "shapes.area", wrong, traces["shapes.area"])
    assert not ok
    assert "mismatch" in feedback


def test_replay_catches_raising_candidate(captured) -> None:
    repo, traces = captured
    boom = "def clamp(x, lo, hi):\n    raise ValueError('nope')\n"
    ok, feedback = replay(repo, "shapes.clamp", boom, traces["shapes.clamp"])
    assert not ok
    assert "raised" in feedback


def test_oracle_factory_shape(captured) -> None:
    repo, traces = captured
    oracle = make_replay_oracle(repo, traces)
    assert oracle("shapes.area", "def area(b):\n    return b.w * b.h\n")[0] is True
    # a component with no captured traces is reported, not silently passed
    ok, feedback = oracle("shapes.unknown", "def unknown():\n    return 1\n")
    assert not ok and "no captured" in feedback
