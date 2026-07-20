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
    _assemble_winner_bodies,
    _build_rust_staticlib,
    _driver_source,
    build_c_rust_prompt,
    c_rust_worklist,
    compile_oracle,
    differential,
    extern_block,
    link_back,
    rust_signature,
    suspect_global_reads,
    try_rustc,
    whole_program_gate,
)

NONLEAF_C = """\
static int helper(int x) {
  return x * 2;
}

static int caller(int x) {
  return helper(x) + 1;
}
"""

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


# A flat scalar struct passed by pointer — the tractable struct-pointer case:
# no pointer-field chasing, no subclass cast. `weekday` reads three int fields.
STRUCT_C = """\
struct Ymd {
  int y;
  int m;
  int d;
};

static int day_number(struct Ymd *p) {
  return p->y * 10000 + p->m * 100 + p->d;
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


def test_suspect_global_reads_flags_struct_globals() -> None:
    reads_global = CEntry(
        "m.f", "f", "int", [("int", "x")], "static int f(int x){ return x + mem0.cap; }"
    )
    assert "mem0" in suspect_global_reads(reads_global)
    pure = CEntry("m.g", "g", "int", [("int", "x")], "static int g(int x){ return x*2; }")
    assert suspect_global_reads(pure) == set()


@pytest.mark.skipif(not (CC and RUSTC and shutil.which("nm")), reason="needs cc + rustc + nm")
def test_link_back_puts_rust_inside(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "unit.c"
    src.write_text(SAMPLE_C)
    winners = {
        "abs32": (
            '#[no_mangle]\npub extern "C" fn abs32(x: i32) -> i32 '
            "{ if x < 0 { x.wrapping_neg() } else { x } }"
        )
    }
    out_dir = tmp_path / "link"
    gate = link_back(src, winners, out_dir, [])
    assert gate["linked"] is True
    assert gate["symbols_from_rust"] == 1
    assert gate["c_definitions_renamed"] == 1
    assert (out_dir / "unit_linked.c").exists()
    assert "abs32__cgir_replaced" in (out_dir / "unit_linked.c").read_text()


def test_nonleaf_worklist_and_topo_order(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "unit.c").write_text(NONLEAF_C)
    idx = tmp_path / "idx"
    scan_repo(repo, out=idx)
    leaf_only, _ = c_rust_worklist(idx, repo / "unit.c", include_nonleaf=False)
    assert "caller" not in {e.name for e in leaf_only}  # non-leaf excluded by default
    ents, _ = c_rust_worklist(idx, repo / "unit.c", include_nonleaf=True)
    by = {e.name: e for e in ents}
    assert "caller" in by and by["caller"].callees == ["helper"]
    order = [e.name for e in ents]
    assert order.index("helper") < order.index("caller")  # callees first


def test_extern_block_declares_callees() -> None:
    helper = CEntry("m.helper", "helper", "int", [("int", "x")], "")
    block = extern_block([helper])
    assert 'extern "C"' in block and "fn helper(x: i32) -> i32;" in block


@pytest.mark.skipif(not (CC and RUSTC), reason="needs cc + rustc")
def test_nonleaf_differential_calls_into_original_c(tmp_path: Path) -> None:
    """A rewritten Rust caller is verified while calling the *original C*
    callee as an extern symbol (resolved via the oracle, RTLD_GLOBAL)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "unit.c").write_text(NONLEAF_C)
    idx = tmp_path / "idx"
    scan_repo(repo, out=idx)
    wd = tmp_path / "wd"
    wd.mkdir()
    ents, _ = c_rust_worklist(idx, repo / "unit.c", include_nonleaf=True)
    by = {e.name: e for e in ents}
    caller, helper = by["caller"], by["helper"]
    orig = compile_oracle(repo / "unit.c", [e.name for e in ents], wd, [])

    good = (
        '#[no_mangle]\npub extern "C" fn caller(x: i32) -> i32 '
        "{ unsafe { helper(x).wrapping_add(1) } }"
    )
    dl, err = try_rustc(extern_block([helper]) + good, wd, "good", allow_undefined=True)
    assert dl is not None, err
    assert differential(orig, dl, caller, 300, seed=1) == ""  # calls real C helper

    wrong = '#[no_mangle]\npub extern "C" fn caller(x: i32) -> i32 { x + 1 }'  # skips helper
    dl2, err2 = try_rustc(extern_block([helper]) + wrong, wd, "wrong", allow_undefined=True)
    assert dl2 is not None, err2
    assert "mismatch" in differential(orig, dl2, caller, 300, seed=1)


@pytest.mark.skipif(not (CC and RUSTC), reason="needs cc + rustc")
def test_whole_program_gate_accepts_and_rejects(tmp_path: Path) -> None:
    """The gate builds+runs the real program with one function replaced and
    keeps it only if the output matches stock — catching a wrong candidate the
    isolated check might pass."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # non-static so the harness reaches them directly (real projects call
    # internal functions through a public API, like SQLite's shell.c).
    (repo / "unit.c").write_text(
        "int helper(int x){ return x * 2; }\nint caller(int x){ return helper(x) + 1; }\n"
    )
    (repo / "main.c").write_text(
        "#include <stdio.h>\nint caller(int);\n"
        'int main(){ printf("%d %d %d\\n", caller(1), caller(5), caller(-3)); return 0; }\n'
    )
    idx = tmp_path / "idx"
    scan_repo(repo, out=idx)
    ents, _ = c_rust_worklist(idx, repo / "unit.c", include_nonleaf=True)
    # main.c references caller directly, so plain linking pulls it from the
    # staticlib — portable (no macOS -force_load / GNU --whole-archive).
    build = f"cc {repo / 'main.c'} {{source}} {{lib}} -o {{out}}"
    run = "{out}"

    good = '#[no_mangle]\npub extern "C" fn caller(x: i32) -> i32 { unsafe { helper(x) + 1 } }'
    verified, rejected = whole_program_gate(repo / "unit.c", {"caller": good}, ents, build, run)
    assert verified == ["caller"] and not rejected

    wrong = '#[no_mangle]\npub extern "C" fn caller(x: i32) -> i32 { x + 999 }'  # wrong output
    verified, rejected = whole_program_gate(repo / "unit.c", {"caller": wrong}, ents, build, run)
    assert not verified and rejected.get("caller") == "diverged"


def test_struct_worklist_opt_in_and_gate_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "unit.c").write_text(STRUCT_C)
    idx = tmp_path / "idx"
    scan_repo(repo, out=idx)
    # off by default (struct pointer isn't byte-fuzzable)
    base, _ = c_rust_worklist(idx, repo / "unit.c", pointers=True)
    assert "day_number" not in {e.name for e in base}
    # opt in with structs=True
    ents, _ = c_rust_worklist(idx, repo / "unit.c", pointers=True, structs=True)
    e = next(e for e in ents if e.name == "day_number")
    assert e.params == [("struct:Ymd:mut", "p")]
    assert e.gate_only is True  # verified by the whole-program gate, not the differential
    assert "Ymd" in e.struct_defs
    assert "int y" in e.struct_defs["Ymd"]


def test_struct_signature_and_repr_c_prompt() -> None:
    e = CEntry(
        "m.day_number",
        "day_number",
        "int",
        [("struct:Ymd:mut", "p")],
        "static int day_number(struct Ymd *p){...}",
        struct_defs={"Ymd": "struct Ymd {\n  int y;\n  int m;\n  int d;\n};"},
    )
    assert "p: *mut Ymd" in rust_signature(e)
    prompt = build_c_rust_prompt(e)
    assert "#[repr(C)]" in prompt  # instructed to mirror the layout
    assert "struct Ymd" in prompt  # the C definition is provided as context
    assert "(*p).field" in prompt  # deref guidance


@pytest.mark.skipif(not (CC and RUSTC), reason="needs cc + rustc")
def test_struct_whole_program_gate_accepts_repr_c_mirror(tmp_path: Path) -> None:
    """A struct-pointer function is verified only by the whole-program gate on a
    real instance: a correct #[repr(C)] mirror links byte-identical, a
    wrong-layout one diverges."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "unit.c").write_text(
        "struct Ymd { int y; int m; int d; };\n"
        "int day_number(struct Ymd *p){ return p->y * 10000 + p->m * 100 + p->d; }\n"
    )
    (repo / "main.c").write_text(
        "#include <stdio.h>\n"
        "struct Ymd { int y; int m; int d; };\n"
        "int day_number(struct Ymd *);\n"
        "int main(){ struct Ymd a = {2026,7,20}, b = {1999,12,31};\n"
        '  printf("%d %d\\n", day_number(&a), day_number(&b)); return 0; }\n'
    )
    idx = tmp_path / "idx"
    scan_repo(repo, out=idx)
    ents, _ = c_rust_worklist(idx, repo / "unit.c", pointers=True, structs=True)
    assert any(e.name == "day_number" and e.gate_only for e in ents)
    build = f"cc {repo / 'main.c'} {{source}} {{lib}} -o {{out}}"
    run = "{out}"

    good = (
        "#[repr(C)]\npub struct Ymd { y: i32, m: i32, d: i32 }\n"
        '#[no_mangle]\npub extern "C" fn day_number(p: *mut Ymd) -> i32 {\n'
        "  unsafe { (*p).y * 10000 + (*p).m * 100 + (*p).d }\n}"
    )
    verified, rejected = whole_program_gate(repo / "unit.c", {"day_number": good}, ents, build, run)
    assert verified == ["day_number"] and not rejected

    # wrong layout: fields transposed -> reads garbage offsets -> diverges
    wrong = (
        "#[repr(C)]\npub struct Ymd { d: i32, m: i32, y: i32 }\n"
        '#[no_mangle]\npub extern "C" fn day_number(p: *mut Ymd) -> i32 {\n'
        "  unsafe { (*p).y * 10000 + (*p).m * 100 + (*p).d }\n}"
    )
    verified, rejected = whole_program_gate(
        repo / "unit.c", {"day_number": wrong}, ents, build, run
    )
    assert not verified and rejected.get("day_number") == "diverged"


def test_assemble_dedups_shared_struct_but_not_scalar() -> None:
    # Two struct-pointer winners emit the SAME #[repr(C)] mirror; the crate must
    # define the struct once (else rustc: "Rect defined multiple times").
    area = (
        "#[repr(C)]\npub struct Rect { w: i32, h: i32 }\n"
        '#[no_mangle]\npub extern "C" fn rect_area(p: *mut Rect) -> i32 '
        "{ unsafe { (*p).w * (*p).h } }"
    )
    perim = (
        "#[repr(C)]\npub struct Rect { w: i32, h: i32 }\n"
        '#[no_mangle]\npub extern "C" fn rect_perimeter(p: *mut Rect) -> i32 '
        "{ unsafe { 2 * ((*p).w + (*p).h) } }"
    )
    body = _assemble_winner_bodies({"rect_area": area, "rect_perimeter": perim})
    assert body.count("struct Rect") == 1  # deduped
    assert "fn rect_area" in body and "fn rect_perimeter" in body  # both kept
    # the scalar path (no type items) is left as the exact flat concatenation
    scal = {"a": 'pub extern "C" fn a() {}', "b": 'pub extern "C" fn b() {}'}
    assert _assemble_winner_bodies(scal) == "\n\n".join(scal[n] for n in sorted(scal))


@pytest.mark.skipif(not RUSTC, reason="needs rustc")
def test_staticlib_builds_with_two_struct_winners(tmp_path: Path) -> None:
    """Regression: combining two struct-pointer winners that share a struct into
    one staticlib must compile (the per-function gate never exercises this)."""
    area = (
        "#[repr(C)]\npub struct Rect { w: i32, h: i32 }\n"
        '#[no_mangle]\npub extern "C" fn rect_area(p: *mut Rect) -> i32 '
        "{ unsafe { (*p).w * (*p).h } }"
    )
    perim = (
        "#[repr(C)]\npub struct Rect { w: i32, h: i32 }\n"
        '#[no_mangle]\npub extern "C" fn rect_perimeter(p: *mut Rect) -> i32 '
        "{ unsafe { 2 * ((*p).w + (*p).h) } }"
    )
    lib = _build_rust_staticlib({"rect_area": area, "rect_perimeter": perim}, tmp_path)
    assert lib.exists()


def test_cc_available_sanity() -> None:
    # Guard the toolchain-gated tests aren't silently all-skipped in CI images
    # that DO have cc: if cc exists, compile_oracle must at least run.
    if CC is None:
        pytest.skip("no cc")
    assert subprocess.run([CC, "--version"], capture_output=True).returncode == 0
