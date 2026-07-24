"""M3 of the FFI core (docs/design-ffi-pipeline.md §6): the Python->Rust pair.
Eligibility parsing / worklist / prompt / signature rendering are pure Python;
the end-to-end replay-verify is toolchain-gated (needs rustc + pytest) and
driven by a FAKE sampler returning hand-written correct/wrong/panicking Rust —
no network, deterministic.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cgir.ffi.sources.python import parse_signature, python_rust_worklist
from cgir.ffi.targets.rust import RUSTBUF_PRELUDE, rust_signature_ir
from cgir.pipeline import scan_repo
from cgir.rewrite_python_rust import build_python_rust_prompt, run_python_rust

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "python_rust_sample"
RUSTC = shutil.which("rustc")


# --- eligibility (pure Python) ----------------------------------------------


def _sig(src: str, symbol: str):
    return parse_signature(src, symbol)


def test_eligible_shapes_parse_to_signatures() -> None:
    sig, reason = _sig("def clamp(x: int, lo: int, hi: int) -> int: ...", "clamp")
    assert reason == "" and sig is not None
    assert [p.scalar for p in sig.params] == ["i64", "i64", "i64"] and sig.ret == "i64"

    sig, _ = _sig("def fnv1a(data: bytes) -> int: ...", "fnv1a")
    assert sig.params[0].kind == "slice" and sig.params[0].text is False and sig.ret == "i64"

    sig, _ = _sig("def shout(s: str) -> str: ...", "shout")
    assert sig.params[0].kind == "slice" and sig.params[0].text is True and sig.ret == "buf:str"

    sig, _ = _sig("def scale(x: float, k: float) -> bool: ...", "scale")
    assert sig.params[0].scalar == "f64" and sig.ret == "bool"


@pytest.mark.parametrize(
    "src,symbol,needle",
    [
        ("def f(xs: list[int]) -> int: ...", "f", "non-scalar"),
        ("def f(x) -> int: ...", "f", "no type annotation"),
        ("def f(x: int) -> None: ...", "f", "void return"),
        ("def f(x: int, y: int = 1) -> int: ...", "f", "default"),
        ("def f(*args: int) -> int: ...", "f", "*args"),
        ("def f(x: int | None) -> int: ...", "f", "non-scalar"),
        ("def f(x: int) -> dict: ...", "f", "unsupported return type"),
        ("@cache\ndef f(x: int) -> int: ...", "f", "decorated"),
        ("def f(self, x: int) -> int: ...", "f", "method"),
        ("def f(x: int, *, y: int) -> int: ...", "f", "keyword-only"),
        ("async def f(x: int) -> int: ...", "f", "async"),  # found by the corpus sweep
    ],
)
def test_ineligible_shapes_rejected_with_reason(src: str, symbol: str, needle: str) -> None:
    sig, reason = _sig(src, symbol)
    assert sig is None and needle in reason


# --- signature rendering + prompt -------------------------------------------


def test_rust_signature_ir_renders_scalars_slices_and_buf() -> None:
    sig, _ = _sig("def clamp(x: int, lo: int, hi: int) -> int: ...", "clamp")
    assert rust_signature_ir("clamp", sig) == (
        '#[no_mangle]\npub extern "C" fn clamp(x: i64, lo: i64, hi: i64) -> i64'
    )
    sig, _ = _sig("def shout(s: str) -> str: ...", "shout")
    line = rust_signature_ir("shout", sig)
    assert "s_ptr: *const u8, s_len: usize" in line and "-> RustBuf" in line


def test_prompt_includes_prelude_and_slice_rule_for_str_return() -> None:
    from cgir.ffi.sources.python import PyEntry

    sig, _ = _sig("def shout(s: str) -> str: ...", "shout")
    e = PyEntry(
        "m.shout", "shout", sig, "def shout(s: str) -> str:\n    return s.upper()\n", "m.py"
    )
    prompt = build_python_rust_prompt(e)
    assert RUSTBUF_PRELUDE in prompt and "cgir_make_buf" in prompt
    assert "from_raw_parts" in prompt  # the (ptr,len) rule
    assert 'pub extern "C" fn shout(s_ptr: *const u8, s_len: usize) -> RustBuf' in prompt


# --- worklist on the committed fixture --------------------------------------


def test_worklist_splits_eligible_from_ineligible(tmp_path: Path) -> None:
    idx = tmp_path / "idx"
    scan_repo(FIXTURE, out=idx)
    entries, excluded = python_rust_worklist(idx, FIXTURE)
    # the four free functions + the two value-self methods
    assert {e.component_id for e in entries} == {
        "mathlib.clamp",
        "mathlib.fnv1a",
        "mathlib.scale",
        "mathlib.shout",
        "mathlib.Rect.area",
        "mathlib.Rect.scaled_area",
    }
    area = next(e for e in entries if e.component_id == "mathlib.Rect.area")
    assert area.sig.self_param == "self"
    assert [(p.name, p.from_self) for p in area.sig.params] == [("h", True), ("w", True)]
    ex = {cid.rsplit(".", 1)[-1]: reason for cid, reason in excluded}
    assert "container" in ex["pick"] or "non-scalar" in ex["pick"]
    assert "annotation" in ex["greet"]
    assert "void return" in ex["noop"]


def test_method_eligibility_and_rejections() -> None:
    from cgir.ffi.sources.python import class_field_types

    cls = (
        "class Box:\n    w: int\n    h: int\n    label: str\n"
        "    def area(self) -> int: return self.w * self.h\n"
        "    def tag(self, p: str) -> str: return p + self.label\n"
    )
    cf = class_field_types(cls)
    assert cf == {"w": "int", "h": "int", "label": "str"}
    sig, _ = parse_signature(cls, "area", cf)
    assert sig.self_param == "self" and sig.ret == "i64"
    assert all(p.from_self for p in sig.params) and {p.name for p in sig.params} == {"w", "h"}
    sig, _ = parse_signature(cls, "tag", cf)  # self-fields + explicit param
    assert [(p.name, p.from_self) for p in sig.params] == [("label", True), ("p", False)]

    def rej(src: str, cf2: dict[str, str] | None) -> str:
        _, r = parse_signature(src, "f", cf2)
        return r

    c = "class C:\n  x: int\n  def f(self) -> int: return {}\n"
    assert "calls a method" in rej(
        c.format("self.g()") + "  def g(self)->int: return 1", {"x": "int"}
    )
    assert "used as a value" in rej(c.format("id(self)"), {"x": "int"})
    assert "not an annotated class field" in rej(c.format("self.y"), {"x": "int"})
    assert "reads no fields" in rej(c.format("42"), {"x": "int"})
    assert "class field annotations unavailable" in rej(c.format("self.x"), None)
    assert "classmethod" in rej("class C:\n  def f(cls) -> int: return 1\n", {})


# --- end-to-end replay-verify (toolchain-gated, fake sampler) ---------------

_CLAMP = (
    '#[no_mangle]\npub extern "C" fn clamp(x: i64, lo: i64, hi: i64) -> i64 '
    "{ if x < lo { lo } else if x > hi { hi } else { x } }"
)
_FNV = (
    '#[no_mangle]\npub extern "C" fn fnv1a(data_ptr: *const u8, data_len: usize) -> i64 {\n'
    "  let s = unsafe { std::slice::from_raw_parts(data_ptr, data_len) };\n"
    "  let mut h: i64 = 0x811c9dc5;\n"
    "  for &b in s { h = ((h ^ (b as i64)).wrapping_mul(0x01000193)) & 0xFFFFFFFF; }\n  h\n}"
)
_SCALE = '#[no_mangle]\npub extern "C" fn scale(x: f64, k: f64) -> f64 { x * k }'
_SHOUT = RUSTBUF_PRELUDE + (
    '#[no_mangle]\npub extern "C" fn shout(s_ptr: *const u8, s_len: usize) -> RustBuf {\n'
    "  let s = unsafe { std::slice::from_raw_parts(s_ptr, s_len) };\n"
    "  let up = match std::str::from_utf8(s) { Ok(t) => t.to_uppercase(), Err(_) => String::new() };\n"
    "  cgir_make_buf(up.into_bytes())\n}"
)
# value-self methods rewritten as free functions of self's fields
_AREA = '#[no_mangle]\npub extern "C" fn area(h: i64, w: i64) -> i64 { w.wrapping_mul(h) }'
_SCALED = (
    '#[no_mangle]\npub extern "C" fn scaled_area(factor: i64, h: i64, w: i64) -> i64 '
    "{ w.wrapping_mul(h).wrapping_mul(factor) }"
)
_ALL = 6  # eligible functions in the fixture


def _sampler(overrides: dict[str, str] | None = None):
    # "fn scaled_area" must be checked before "fn scale" (substring); dict order.
    table = {
        "fn scaled_area": _SCALED,
        "fn area": _AREA,
        "fn clamp": _CLAMP,
        "fn fnv1a": _FNV,
        "fn scale": _SCALE,
        "fn shout": _SHOUT,
    }
    table.update(overrides or {})

    def sample(prompt: str, model: str) -> tuple[str, float]:
        for needle, code in table.items():
            if needle in prompt:
                return code, 0.0
        return "// none", 0.0

    return sample


@pytest.fixture(scope="module")
def fixture_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    idx = tmp_path_factory.mktemp("prs") / "idx"
    scan_repo(FIXTURE, out=idx)
    return idx


@pytest.mark.skipif(not RUSTC, reason="needs rustc")
def test_end_to_end_all_solved(fixture_index: Path) -> None:
    from cgir.replay import capture

    entries, _ = python_rust_worklist(fixture_index, FIXTURE)
    traces = capture(FIXTURE, {e.component_id: (Path(e.path), e.symbol) for e in entries})
    report = run_python_rust(fixture_index, FIXTURE, sampler=_sampler(), traces=traces, k=1)
    assert report["totals"]["solved"] == _ALL
    assert {o["component_id"] for o in report["results"] if o["status"] == "solved"} == {
        "mathlib.clamp",
        "mathlib.fnv1a",
        "mathlib.scale",
        "mathlib.shout",
        "mathlib.Rect.area",  # value-self methods, rewritten as free functions
        "mathlib.Rect.scaled_area",
    }
    assert all(o.get("verify") == "replay" for o in report["results"])


@pytest.mark.skipif(not RUSTC, reason="needs rustc")
def test_end_to_end_wrong_candidate_rejected(fixture_index: Path) -> None:
    from cgir.replay import capture

    entries, _ = python_rust_worklist(fixture_index, FIXTURE)
    traces = capture(FIXTURE, {e.component_id: (Path(e.path), e.symbol) for e in entries})
    wrong = '#[no_mangle]\npub extern "C" fn clamp(x: i64, lo: i64, hi: i64) -> i64 { x }'
    report = run_python_rust(
        fixture_index, FIXTURE, sampler=_sampler({"fn clamp": wrong}), traces=traces, k=1
    )
    clamp = next(o for o in report["results"] if o["component_id"] == "mathlib.clamp")
    assert clamp["status"] == "unsolved"
    assert any("replay mismatch" in a["feedback"] for a in clamp["attempts"])


@pytest.mark.skipif(not RUSTC, reason="needs rustc")
def test_end_to_end_panic_candidate_rejected_not_harness_death(fixture_index: Path) -> None:
    from cgir.replay import capture

    entries, _ = python_rust_worklist(fixture_index, FIXTURE)
    traces = capture(FIXTURE, {e.component_id: (Path(e.path), e.symbol) for e in entries})
    boom = (
        '#[no_mangle]\npub extern "C" fn clamp(x: i64, lo: i64, hi: i64) -> i64 { panic!("boom") }'
    )
    report = run_python_rust(
        fixture_index, FIXTURE, sampler=_sampler({"fn clamp": boom}), traces=traces, k=1
    )
    clamp = next(o for o in report["results"] if o["component_id"] == "mathlib.clamp")
    assert clamp["status"] == "unsolved"
    assert any("replay crash" in a["feedback"] for a in clamp["attempts"])


# --- M4: apply (wrapper emission + splice + gate) ----------------------------


def test_assemble_dedups_prelude_across_string_winners() -> None:
    from cgir.ffi.targets.rust import RUSTBUF_PRELUDE, assemble_python_winners

    w1 = (
        RUSTBUF_PRELUDE
        + '#[no_mangle]\npub extern "C" fn a(p: *const u8, n: usize) -> RustBuf { todo!() }'
    )
    w2 = (
        RUSTBUF_PRELUDE
        + '#[no_mangle]\npub extern "C" fn b(p: *const u8, n: usize) -> RustBuf { todo!() }'
    )
    body = assemble_python_winners({"a": w1, "b": w2})
    assert body.count("struct RustBuf") == 1  # type deduped
    assert body.count("fn cgir_buf_free") == 1  # helper fn deduped
    assert body.count("fn cgir_make_buf") == 1
    assert "fn a(" in body and "fn b(" in body  # both winners kept


def test_dedup_traces_keeps_distinct_inputs() -> None:
    from cgir.rewrite_python_rust import _dedup_traces

    # a hot leaf gets called many times on few distinct inputs (markupsafe:
    # 80k calls, 26 distinct) — dedup keeps one per argument tuple
    traces = [(("a",), "A"), (("a",), "A"), (("b",), "B"), (("a",), "A"), (("c",), "C")]
    assert _dedup_traces(traces) == [(("a",), "A"), (("b",), "B"), (("c",), "C")]
    # bytearray args are unhashable but keyed by their bytes
    ba = [((bytearray(b"x"),), 1), ((bytearray(b"x"),), 1), ((bytearray(b"y"),), 2)]
    assert len(_dedup_traces(ba)) == 2


CARGO = shutil.which("cargo")


def test_render_pyo3_shim_shapes() -> None:
    from cgir.ffi.sources.python import PyEntry
    from cgir.ffi.targets.pyo3 import render_pyo3_shim

    sig, _ = _sig("def clamp(x: int, lo: int, hi: int) -> int: ...", "clamp")
    sh = render_pyo3_shim(PyEntry("m.clamp", "clamp", sig, "", "m.py"))
    assert '#[pyo3(name = "clamp")]' in sh
    assert "fn py_clamp(x: i64, lo: i64, hi: i64) -> i64" in sh
    assert "unsafe { clamp(x, lo, hi) }" in sh  # calls the verified extern fn

    sig, _ = _sig("def shout(s: str) -> str: ...", "shout")
    sh = render_pyo3_shim(PyEntry("m.shout", "shout", sig, "", "m.py"))
    assert "s: &str" in sh and "-> String" in sh
    assert "s.as_ptr(), s.len()" in sh and "cgir_buf_free" in sh

    sig, _ = _sig("def enc(b: bytes) -> bytes: ...", "enc")
    sh = render_pyo3_shim(PyEntry("m.enc", "enc", sig, "", "m.py"))
    assert "py: Python<'py>" in sh and "-> Bound<'py, PyBytes>" in sh and "b: &[u8]" in sh


@pytest.mark.skipif(not (RUSTC and CARGO), reason="needs rustc + cargo")
def test_pyo3_extension_builds_and_runs(tmp_path: Path) -> None:
    import importlib.util

    from cgir.ffi.sources.python import PyEntry
    from cgir.ffi.targets.pyo3 import build_pyo3_extension, extension_filename

    sig_c, _ = _sig("def clamp(x: int, lo: int, hi: int) -> int: ...", "clamp")
    sig_s, _ = _sig("def shout(s: str) -> str: ...", "shout")
    entries = [
        PyEntry("m.clamp", "clamp", sig_c, "", "m.py"),
        PyEntry("m.shout", "shout", sig_s, "", "m.py"),
    ]
    lib, err = build_pyo3_extension(
        {"clamp": _CLAMP, "shout": _SHOUT}, entries, "_cgir_test_ext", tmp_path
    )
    assert lib is not None, err
    so = tmp_path / extension_filename("_cgir_test_ext")
    shutil.copy(lib, so)
    spec = importlib.util.spec_from_file_location("_cgir_test_ext", so)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # the verified extern "C" Rust, reached natively — scalar and RustBuf->String
    assert mod.clamp(5, 0, 10) == 5 and mod.clamp(-3, 0, 10) == 0
    assert mod.shout("hi") == "HI" and mod.shout("straße") == "STRASSE"


def test_render_python_wrapper_preserves_name_and_params() -> None:
    from cgir.ffi.sources.python import PyEntry
    from cgir.rewrite_python_rust import render_python_wrapper

    sig, _ = _sig("def clamp(x: int, lo: int, hi: int) -> int: ...", "clamp")
    e = PyEntry("m.clamp", "clamp", sig, "", "m.py")
    w = render_python_wrapper(e)
    assert w.startswith("def clamp(x, lo, hi):")
    assert "from _cgir_rs import clamp as _rs" in w and "return _rs(x, lo, hi)" in w


@pytest.mark.skipif(not RUSTC, reason="needs rustc")
def test_apply_splices_wrappers_and_repo_tests_pass_with_rust_inside(tmp_path: Path) -> None:
    import shutil

    from cgir.replay import capture

    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    idx = tmp_path / "idx"
    scan_repo(repo, out=idx)
    entries, _ = python_rust_worklist(idx, repo)
    traces = capture(repo, {e.component_id: (Path(e.path), e.symbol) for e in entries})

    report = run_python_rust(idx, repo, sampler=_sampler(), traces=traces, k=1, apply=True)
    gate = report["final_gate"]
    assert gate["applied"] == _ALL
    assert gate["tests_ok"] is True  # the repo's OWN pytest passes with Rust inside
    assert gate["hard_drift_outside_rewritten"] == []  # drift is only on the rewritten set
    assert (repo / "_cgir_rs.py").exists() and list(repo.glob("_cgir_rs_lib.*"))
    # the original body was replaced by a delegating wrapper (incl the value-self
    # method, which reads its fields off self)
    spliced = (repo / "mathlib.py").read_text()
    assert "from _cgir_rs import area as _rs" in spliced and "_rs(self.h, self.w)" in spliced
    assert "from _cgir_rs import clamp as _rs" in spliced
