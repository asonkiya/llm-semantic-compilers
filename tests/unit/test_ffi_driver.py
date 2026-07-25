"""The differential driver's liveness guarantees (:mod:`cgir.ffi.driver`).

The regression pinned here came from the SQLite TU battery: a candidate that
mass-writes through a wild pointer (fuzzed size params run to 2^31) put the
driver in a page-fault storm, and the Python-side ``subprocess.run(timeout=)``
failed to unwedge it — the parent blocked 11+ minutes past its own deadline.
The layer that holds is the driver's *own* ``alarm()``: SIGALRM is raised
in-kernel by the child's timer and needs nothing from the parent, so the child
dies, the pipe closes, and the parent wakes on the exit event.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from cgir.ffi import driver
from cgir.ffi.driver import DRIVER_ALARM_S, _driver_source, differential
from cgir.ffi.ir import CEntry


def _entry() -> CEntry:
    return CEntry(
        component_id="x",
        name="fill",
        ret="void",
        params=[("ptr:buf:mut", "buf"), ("int", "n")],
        source="",
    )


def test_driver_source_has_self_deadline():
    """The generated driver arms alarm() before the fuzz loop, with a deadline
    below the Python-side subprocess timeout (120s) so the child exits first."""
    src = _driver_source(_entry())
    assert f"alarm({DRIVER_ALARM_S});" in src
    assert "#include <unistd.h>" in src
    assert DRIVER_ALARM_S < 120
    # the alarm must NOT be trapped by the fault handlers — SIGALRM's default
    # action (terminate) is the whole point.
    assert "SIGALRM" not in src.split("install_handlers")[1].split("}")[0]


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
def test_differential_self_terminates_on_nonterminating_candidate(tmp_path, monkeypatch):
    """A candidate that never returns must yield a timeout *rejection* from the
    driver's own alarm — not hang the harness. Deadline shrunk to 2s so the
    test runs fast; the verdict message names the self-termination."""
    monkeypatch.setattr(driver, "DRIVER_ALARM_S", 2)
    (tmp_path / "orig.c").write_text(
        "void fill(char *buf, int n) { for (int i = 0; i < n && i < 64; i++) buf[i] = 1; }\n"
    )
    (tmp_path / "cand.c").write_text(
        "void fill(char *buf, int n) { volatile int i = 0; while (1) { i++; } }\n"
    )
    for stem in ("orig", "cand"):
        r = subprocess.run(
            ["cc", "-shared", "-o", str(tmp_path / f"{stem}.so"), str(tmp_path / f"{stem}.c")],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
    verdict = differential(tmp_path / "orig.so", tmp_path / "cand.so", _entry(), n=50, seed=1)
    assert "self-terminated" in verdict, verdict


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
def test_differential_still_passes_equivalent_candidate(tmp_path):
    """The alarm must not break the happy path: an equivalent pair still
    verifies as such (empty verdict) well inside the deadline."""
    body = "void fill(char *buf, int n) { for (int i = 0; i < n && i < 64; i++) buf[i] = (char)(i * 7); }\n"
    (tmp_path / "orig.c").write_text(body)
    (tmp_path / "cand.c").write_text(body)
    for stem in ("orig", "cand"):
        r = subprocess.run(
            ["cc", "-shared", "-o", str(tmp_path / f"{stem}.so"), str(tmp_path / f"{stem}.c")],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
    verdict = differential(tmp_path / "orig.so", tmp_path / "cand.so", _entry(), n=50, seed=1)
    assert verdict == "", verdict
