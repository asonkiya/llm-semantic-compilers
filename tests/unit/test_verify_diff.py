"""verify-diff: prove a change is behavior-preserving against recorded inputs.

The three honest buckets are pinned — preserving (agrees), diverged (with a
counterexample), unverified (couldn't check, with the reason) — plus the git+ast
layer that finds what changed, and fuzzing around recorded inputs to catch a
divergence the exact recorded input misses.
"""

from __future__ import annotations

import subprocess

import pytest

from cgir.verify_diff import (
    DIVERGED,
    PRESERVING,
    UNVERIFIED,
    changed_py_functions,
    differential_replay,
    fuzz_around,
    verify_diff,
)


@pytest.fixture
def repo(tmp_path):
    """A tiny git repo with a module + a test that exercises it, and one commit
    as the base ref."""
    (tmp_path / "mathlib.py").write_text(
        "def scale(x, k):\n    return x * k\n\n\ndef clamp(x, lo, hi):\n"
        "    return lo if x < lo else hi if x > hi else x\n"
    )
    (tmp_path / "test_mathlib.py").write_text(
        "from mathlib import scale, clamp\n"
        "def test_scale():\n    assert scale(3, 4) == 12\n    assert scale(-2, 5) == -10\n"
        "def test_clamp():\n    assert clamp(5, 0, 10) == 5\n    assert clamp(-1, 0, 10) == 0\n"
    )
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        check=True,
    )
    return tmp_path


# --- the core differential --------------------------------------------------


def test_preserving_when_equivalent(tmp_path):
    old = "def f(x, k):\n    return x * k\n"
    new = "def f(x, k):\n    return k * x\n"  # commutative — same behavior
    (tmp_path / "m.py").write_text("def f(x, k):\n    return x * k\n")
    v = differential_replay(tmp_path, "m.f", old, new, [(3, 4), (-2, 5), (0, 9)])
    assert v.status == PRESERVING and v.checked == 3


def test_diverged_gives_counterexample(tmp_path):
    old = "def f(x, k):\n    return x * k\n"
    new = "def f(x, k):\n    return x + k\n"  # wrong
    (tmp_path / "m.py").write_text("def f(x, k):\n    return x * k\n")
    v = differential_replay(tmp_path, "m.f", old, new, [(3, 4), (2, 2)])
    assert v.status == DIVERGED
    assert "(3, 4)" in v.detail and "12" in v.detail and "7" in v.detail  # old=12 new=7


def test_matching_exceptions_are_preserving(tmp_path):
    old = "def f(x):\n    return 1 // x\n"
    new = "def f(x):\n    return 1 // x if x else 1 // x\n"  # both ZeroDivisionError on 0
    (tmp_path / "m.py").write_text("def f(x):\n    return 1 // x\n")
    v = differential_replay(tmp_path, "m.f", old, new, [(2,), (0,)])
    assert v.status == PRESERVING


def test_new_raises_where_old_returned_is_divergence(tmp_path):
    old = "def f(x):\n    return x or 0\n"
    new = "def f(x):\n    return 1 // x\n"  # raises on 0
    (tmp_path / "m.py").write_text("def f(x):\n    return x\n")
    v = differential_replay(tmp_path, "m.f", old, new, [(0,)])
    assert v.status == DIVERGED and "ZeroDivisionError" in v.detail


def test_no_inputs_is_unverified_not_a_pass(tmp_path):
    (tmp_path / "m.py").write_text("def f(x):\n    return x\n")
    v = differential_replay(
        tmp_path, "m.f", "def f(x):\n    return x\n", "def f(x):\n    return x\n", []
    )
    assert v.status == UNVERIFIED and "not exercised" in v.detail


# --- fuzzing around recorded inputs -----------------------------------------


def test_fuzz_finds_divergence_tests_miss(tmp_path):
    # equal on the recorded input (5) but differ at the boundary (0)
    old = "def f(x):\n    return 100 // x if x else 0\n"
    new = "def f(x):\n    return 100 // x\n"  # raises on 0, which fuzzing hits
    (tmp_path / "m.py").write_text("def f(x):\n    return x\n")
    recorded = [(5,)]
    # exact recorded input agrees...
    assert differential_replay(tmp_path, "m.f", old, new, recorded).status == PRESERVING
    # ...but fuzzing around it surfaces the 0 divergence
    fuzzed = fuzz_around(recorded, rounds=11)
    assert (0,) in fuzzed
    v = differential_replay(tmp_path, "m.f", old, new, recorded + fuzzed)
    assert v.status == DIVERGED and "ZeroDivisionError" in v.detail


def test_fuzz_around_mutates_by_type():
    got = fuzz_around([(3, "hi", True)], rounds=3)
    assert any(a[0] == 0 for a in got)  # int edge
    assert any(a[1] == "" for a in got)  # str edge
    assert any(a[2] is False for a in got)  # bool flip


# --- the git + ast layer ----------------------------------------------------


def test_changed_functions_detects_the_edit(repo):
    # change scale, leave clamp untouched
    (repo / "mathlib.py").write_text(
        "def scale(x, k):\n    return x + k\n\n\ndef clamp(x, lo, hi):\n"
        "    return lo if x < lo else hi if x > hi else x\n"
    )
    changed, _notes = changed_py_functions(repo, "HEAD")
    names = {c.qualname for c in changed}
    assert "mathlib.scale" in names and "mathlib.clamp" not in names
    scale = next(c for c in changed if c.qualname == "mathlib.scale")
    assert "x * k" in scale.old_src and "x + k" in scale.new_src


def test_changed_functions_bad_ref_is_a_note_not_a_crash(repo):
    changed, notes = changed_py_functions(repo, "no-such-ref")
    assert changed == [] and any("valid ref" in n for n in notes)


# --- end to end (needs pytest to record inputs) -----------------------------


def test_verify_diff_end_to_end_flags_a_real_regression(repo):
    (repo / "mathlib.py").write_text(
        "def scale(x, k):\n    return x + k\n\n\ndef clamp(x, lo, hi):\n"  # scale broken
        "    return lo if x < lo else hi if x > hi else x\n"
    )
    report = verify_diff(repo, "HEAD")
    by = {v.qualname: v for v in report.verdicts}
    assert by["mathlib.scale"].status == DIVERGED
    assert not report.ok  # the merge gate would block this


def test_verify_diff_passes_a_behavior_preserving_refactor(repo):
    (repo / "mathlib.py").write_text(
        "def scale(x, k):\n    result = k * x  # refactored, same behavior\n    return result\n"
        "\n\ndef clamp(x, lo, hi):\n    return lo if x < lo else hi if x > hi else x\n"
    )
    report = verify_diff(repo, "HEAD")
    by = {v.qualname: v for v in report.verdicts}
    assert by["mathlib.scale"].status == PRESERVING
    assert report.ok


def test_systemexit_candidate_does_not_crash(tmp_path):
    """A CLI/argparse function raises SystemExit (a BaseException). It must be a
    caught outcome, not a crash of the whole run — the slugify rewrite pass
    found this escaping and killing the process."""
    (tmp_path / "m.py").write_text("def f(x):\n    return x\n")
    old = "def f(x):\n    return x\n"
    new = "def f(x):\n    import sys\n    sys.exit(2)\n"  # SystemExit
    v = differential_replay(tmp_path, "m.f", old, new, [(1,)])
    assert v.status == DIVERGED and "SystemExit" in v.detail
    both = "def f(x):\n    import sys\n    sys.exit(1)\n"
    assert differential_replay(tmp_path, "m.f", both, both, [(1,)]).status == PRESERVING
