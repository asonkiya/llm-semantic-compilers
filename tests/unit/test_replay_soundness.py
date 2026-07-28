"""Soundness pins for the capture/replay stack.

Each test here pins a confirmed false-pass (or false-fail) vector: a way a
behavior change could be verified as "preserving", or a correct candidate
rejected. The replay oracle and verify-diff share this machinery, so a hole
here is a hole in the flagship guarantee.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from cgir.replay import _eq, capture, replay
from cgir.verify_diff import (
    DIVERGED,
    PRESERVING,
    changed_py_functions,
    verify_diff,
)


def _init(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)


def _commit(repo: Path, msg: str = "base") -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    cfg = ["-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "-C", str(repo), *cfg, "commit", "-q", "-m", msg], check=True)


# --- _eq: numeric comparisons must not coerce ---------------------------------


class TestEqExactness:
    def test_distinct_big_ints_are_not_equal(self):
        # isclose(float(a), float(b)) collapses ints beyond 2**53 — hash/ID/
        # checksum divergences vanish exactly where the fuzzer probes (2**63-1).
        assert not _eq(2**63 - 1, 2**63 - 2)
        assert not _eq(10**12, 10**12 + 1000)

    def test_equal_ints_are_equal(self):
        assert _eq(2**63 - 1, 2**63 - 1)
        assert _eq(-5, -5)
        assert _eq(0, 0)

    def test_int_vs_float_is_a_type_change(self):
        assert not _eq(1, 1.0)
        assert not _eq(0, 0.0)

    def test_bool_vs_int_is_a_type_change(self):
        assert not _eq(True, 1)
        assert not _eq(False, 0)

    def test_floats_keep_tolerance_and_nan(self):
        assert _eq(0.1 + 0.2, 0.3)
        assert _eq(float("nan"), float("nan"))
        assert not _eq(1.0, 2.0)

    def test_exactness_reaches_into_containers(self):
        assert not _eq([2**63 - 1], [2**63 - 2])
        assert not _eq({"k": 1}, {"k": 1.0})


# --- capture: a raising call is not a call that returned None -----------------


def test_capture_distinguishes_raise_from_return_none(tmp_path):
    """setprofile's return event fires with arg=None on exception exit; recording
    that as a real (args, None) trace made a wrong None-returning candidate pass
    and the correct raising candidate fail."""
    (tmp_path / "m.py").write_text(
        "def f(x):\n    if x < 0:\n        raise ValueError('neg')\n    return None\n"
    )
    (tmp_path / "test_m.py").write_text(
        "import pytest\n"
        "from m import f\n"
        "def test_ok():\n    assert f(3) is None\n"
        "def test_raises():\n"
        "    with pytest.raises(ValueError):\n        f(-5)\n"
    )
    traces = capture(tmp_path, {"m.f": (Path("m.py"), "f")})
    got = traces["m.f"]
    assert len(got) == 2

    correct = "def f(x):\n    if x < 0:\n        raise ValueError('neg')\n    return None\n"
    ok, why = replay(tmp_path, "m.f", correct, got)
    assert ok, f"candidate identical to the original must pass: {why}"

    wrong = "def f(x):\n    return None\n"
    ok, why = replay(tmp_path, "m.f", wrong, got)
    assert not ok, "a candidate that swallows the raise must not pass"


def test_capture_raise_requires_same_exception_type(tmp_path):
    (tmp_path / "m.py").write_text(
        "def f(x):\n    if x < 0:\n        raise ValueError('neg')\n    return x\n"
    )
    (tmp_path / "test_m.py").write_text(
        "import pytest\n"
        "from m import f\n"
        "def test_raises():\n"
        "    with pytest.raises(ValueError):\n        f(-5)\n"
    )
    traces = capture(tmp_path, {"m.f": (Path("m.py"), "f")})
    wrong_type = "def f(x):\n    raise KeyError('nope')\n"
    ok, _ = replay(tmp_path, "m.f", wrong_type, traces["m.f"])
    assert not ok


def test_internally_caught_exception_still_records_the_return(tmp_path):
    """A raise that the function itself catches is a normal return — the
    raise-detection must not misfile it."""
    (tmp_path / "m.py").write_text(
        "def f(x):\n"
        "    try:\n"
        "        return 10 // x\n"
        "    except ZeroDivisionError:\n"
        "        return -1\n"
    )
    (tmp_path / "test_m.py").write_text(
        "from m import f\ndef test_f():\n    assert f(2) == 5\n    assert f(0) == -1\n"
    )
    traces = capture(tmp_path, {"m.f": (Path("m.py"), "f")})
    got = traces["m.f"]
    assert len(got) == 2
    ok, why = replay(
        tmp_path,
        "m.f",
        "def f(x):\n"
        "    try:\n"
        "        return 10 // x\n"
        "    except ZeroDivisionError:\n"
        "        return -1\n",
        got,
    )
    assert ok, why


# --- verify_diff: old code must run against OLD module state ------------------


def test_changed_module_constant_not_masked(tmp_path):
    """The old function must execute against the old module state. Importing the
    working tree and exec'ing the old body into it ran old code against NEW
    globals — a 6→30 behavior change reported as preserving."""
    (tmp_path / "m.py").write_text("SCALE = 2\n\n\ndef times(x):\n    return x * SCALE\n")
    (tmp_path / "test_m.py").write_text(
        "from m import times\ndef test_times():\n    assert times(3) in (6, 30)\n"
    )
    _init(tmp_path)
    _commit(tmp_path)
    (tmp_path / "m.py").write_text("SCALE = 10\n\n\ndef times(x):\n    return x * SCALE + 0\n")

    report = verify_diff(tmp_path, "HEAD")
    by = {v.qualname: v for v in report.verdicts}
    assert by["m.times"].status == DIVERGED, by["m.times"]


def test_changed_helper_function_not_masked(tmp_path):
    """Same hole via a changed same-file helper the target calls."""
    (tmp_path / "m.py").write_text(
        "def _rate():\n    return 2\n\n\ndef price(x):\n    return x * _rate()\n"
    )
    (tmp_path / "test_m.py").write_text(
        "from m import price\ndef test_price():\n    assert price(3) in (6, 30)\n"
    )
    _init(tmp_path)
    _commit(tmp_path)
    (tmp_path / "m.py").write_text(
        "def _rate():\n    return 10\n\n\ndef price(x):\n    return x * _rate() + 0\n"
    )

    report = verify_diff(tmp_path, "HEAD")
    by = {v.qualname: v for v in report.verdicts}
    assert by["m.price"].status == DIVERGED, by["m.price"]


# --- verify_diff: changed class methods must actually get verified ------------


def test_changed_method_preserving(tmp_path):
    (tmp_path / "m.py").write_text(
        "class Calc:\n"
        "    def __init__(self, base):\n"
        "        self.base = base\n\n"
        "    def add(self, x):\n"
        "        return self.base + x\n"
    )
    (tmp_path / "test_m.py").write_text(
        "from m import Calc\ndef test_add():\n    assert Calc(10).add(5) == 15\n"
    )
    _init(tmp_path)
    _commit(tmp_path)
    (tmp_path / "m.py").write_text(
        "class Calc:\n"
        "    def __init__(self, base):\n"
        "        self.base = base\n\n"
        "    def add(self, x):\n"
        "        return x + self.base\n"
    )

    report = verify_diff(tmp_path, "HEAD")
    by = {v.qualname: v for v in report.verdicts}
    v = by["m.Calc.add"]
    assert v.status == PRESERVING, v


def test_changed_method_diverged(tmp_path):
    (tmp_path / "m.py").write_text(
        "class Calc:\n"
        "    def __init__(self, base):\n"
        "        self.base = base\n\n"
        "    def add(self, x):\n"
        "        return self.base + x\n"
    )
    (tmp_path / "test_m.py").write_text(
        "from m import Calc\ndef test_add():\n    assert Calc(10).add(5) in (15, 16)\n"
    )
    _init(tmp_path)
    _commit(tmp_path)
    (tmp_path / "m.py").write_text(
        "class Calc:\n"
        "    def __init__(self, base):\n"
        "        self.base = base\n\n"
        "    def add(self, x):\n"
        "        return self.base + x + 1\n"
    )

    report = verify_diff(tmp_path, "HEAD")
    by = {v.qualname: v for v in report.verdicts}
    v = by["m.Calc.add"]
    assert v.status == DIVERGED, v


# --- changed-file discovery must survive spaces in paths ----------------------


def test_spaced_filename_is_not_silently_skipped(tmp_path):
    (tmp_path / "a b.py").write_text("def f(x):\n    return x\n")
    _init(tmp_path)
    _commit(tmp_path)
    (tmp_path / "a b.py").write_text("def f(x):\n    return x + 1\n")

    changed, _notes = changed_py_functions(tmp_path, "HEAD")
    assert any(cf.path == "a b.py" for cf in changed), (
        "whitespace-split git output silently dropped the file — no verdict at all"
    )


# --- CLI: --strict turns all-unverified into a failure ------------------------


def test_verify_diff_cli_strict_fails_on_unverified(tmp_path):
    from typer.testing import CliRunner

    from cgir.cli import app

    # f is changed but no test ever calls it -> unverified.
    (tmp_path / "m.py").write_text("def f(x):\n    return x\n")
    (tmp_path / "test_m.py").write_text("def test_unrelated():\n    assert True\n")
    _init(tmp_path)
    _commit(tmp_path)
    (tmp_path / "m.py").write_text("def f(x):\n    return x + 1\n")

    runner = CliRunner()
    default = runner.invoke(app, ["verify-diff", "HEAD", "--repo", str(tmp_path)])
    assert default.exit_code == 0, default.output  # documented default: warn only
    strict = runner.invoke(app, ["verify-diff", "HEAD", "--repo", str(tmp_path), "--strict"])
    assert strict.exit_code != 0, strict.output
