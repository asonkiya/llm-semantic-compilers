"""`cgir rewrite --lang c-rust` engine — worklist parsing, prompt/driver
codegen, and (toolchain-gated) an end-to-end differential on a tiny C unit.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from cgir.pipeline import scan_repo
from cgir.rewrite_c_rust import (
    CEntry,
    _driver_source,
    build_c_rust_prompt,
    c_rust_worklist,
    compile_oracle,
    differential,
    rust_signature,
    try_rustc,
)

CC = shutil.which("cc")
RUSTC = shutil.which("rustc")

SAMPLE_C = """\
static int abs32(int x) {
  if (x < 0) return -x;
  return x;
}

static unsigned int add_mod(unsigned int a, unsigned int b) {
  return a + b;
}

static int strlen_c(const char *z) {
  int n = 0;
  while (z[n]) n++;
  return n;
}
"""


def _index(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "unit.c").write_text(SAMPLE_C)
    idx = tmp_path / "idx"
    scan_repo(repo, out=idx)
    return repo, idx


def test_worklist_scalar_only(tmp_path: Path) -> None:
    repo, idx = _index(tmp_path)
    entries, _ = c_rust_worklist(idx, repo / "unit.c", pointers=False)
    names = {e.name for e in entries}
    assert "abs32" in names and "add_mod" in names
    assert "strlen_c" not in names  # pointer param, pointers=False


def test_worklist_pointers_opt_in(tmp_path: Path) -> None:
    repo, idx = _index(tmp_path)
    entries, _ = c_rust_worklist(idx, repo / "unit.c", pointers=True)
    e = next(e for e in entries if e.name == "strlen_c")
    assert e.params == [("ptr:str:const", "z")]
    assert e.ret == "int"


def test_rust_signature_and_prompt(tmp_path: Path) -> None:
    e = CEntry("m.abs32", "abs32", "int", [("int", "x")], "static int abs32(int x){...}")
    sig = rust_signature(e)
    assert sig == '#[no_mangle]\npub extern "C" fn abs32(x: i32) -> i32'
    prompt = build_c_rust_prompt(e, "MAXVAL = 7")
    assert "abs32" in prompt and "MAXVAL = 7" in prompt and "```c" in prompt


def test_pointer_signature_and_ptr_rule() -> None:
    e = CEntry("m.f", "f", "int", [("ptr:str:const", "z")], "static int f(const char*z){...}")
    assert "z: *const u8" in rust_signature(e)
    assert "Pointer params are raw C pointers" in build_c_rust_prompt(e)


def test_driver_source_is_wellformed() -> None:
    e = CEntry("m.abs32", "abs32", "int", [("int", "x")], "")
    src = _driver_source(e)
    assert "install_handlers" in src and "sigaltstack" in src
    assert 'dlsym(ho, "abs32")' in src
    assert src.count("int main(") == 1


@pytest.mark.skipif(not (CC and RUSTC), reason="needs cc + rustc")
def test_end_to_end_differential(tmp_path: Path) -> None:
    repo, idx = _index(tmp_path)
    wd = tmp_path / "wd"
    wd.mkdir()
    entries, _ = c_rust_worklist(idx, repo / "unit.c", pointers=False)
    e = next(e for e in entries if e.name == "abs32")
    orig = compile_oracle(repo / "unit.c", [e.name], wd, [])

    good = (
        '#[no_mangle]\npub extern "C" fn abs32(x: i32) -> i32 '
        "{ if x < 0 { x.wrapping_neg() } else { x } }"
    )
    dl, err = try_rustc(good, wd, "good")
    assert dl is not None, err
    assert differential(orig, dl, e, 500, seed=1) == ""  # equivalent

    wrong = '#[no_mangle]\npub extern "C" fn abs32(x: i32) -> i32 { x }'
    dl, err = try_rustc(wrong, wd, "wrong")
    assert dl is not None, err
    assert "mismatch" in differential(orig, dl, e, 500, seed=1)


@pytest.mark.skipif(not CC, reason="needs cc")
def test_pointer_differential_catches_wrong_strlen(tmp_path: Path) -> None:
    if not RUSTC:
        pytest.skip("needs rustc")
    repo, idx = _index(tmp_path)
    wd = tmp_path / "wd"
    wd.mkdir()
    entries, _ = c_rust_worklist(idx, repo / "unit.c", pointers=True)
    e = next(e for e in entries if e.name == "strlen_c")
    orig = compile_oracle(repo / "unit.c", [e.name], wd, [])
    good = (
        '#[no_mangle]\npub extern "C" fn strlen_c(z: *const u8) -> i32 {\n'
        "  if z.is_null() { return 0; }\n  let mut n = 0i32;\n"
        "  unsafe { while *z.offset(n as isize) != 0 { n += 1; } }\n  n\n}"
    )
    dl, err = try_rustc(good, wd, "s")
    assert dl is not None, err
    assert differential(orig, dl, e, 500, seed=1) == ""


def test_cc_available_sanity() -> None:
    # Guard the toolchain-gated tests aren't silently all-skipped in CI images
    # that DO have cc: if cc exists, compile_oracle must at least run.
    if CC is None:
        pytest.skip("no cc")
    assert subprocess.run([CC, "--version"], capture_output=True).returncode == 0
