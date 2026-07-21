"""M2 of the FFI core (docs/design-ffi-pipeline.md §6): the ReplayOracle —
recorded (args, result) traces replayed against a compiled Rust cdylib over
ctypes. Toolchain-gated tests run against a hand-written fixture crate built
with `panic=abort` (the production convention); validation tests are pure
Python and always run.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from cgir.ffi.ir import Param, Signature
from cgir.ffi.replay_ffi import (
    _run_batches,
    replay_against_dylib,
    validate_traces,
)

RUSTC = shutil.which("rustc")

# The fixture exercises every marshalling convention: scalar i64/f64/bool,
# (ptr,len) slices, the RustBuf{ptr,len,cap} return + cgir_buf_free pairing,
# a panicking input (SIGABRT under panic=abort), and a non-terminating input.
FIXTURE_RS = """\
#[repr(C)]
pub struct RustBuf { ptr: *mut u8, len: usize, cap: usize }

fn make_buf(v: Vec<u8>) -> RustBuf {
    let mut v = std::mem::ManuallyDrop::new(v);
    RustBuf { ptr: v.as_mut_ptr(), len: v.len(), cap: v.capacity() }
}

#[no_mangle]
pub extern "C" fn cgir_buf_free(b: RustBuf) {
    if !b.ptr.is_null() {
        unsafe { drop(Vec::from_raw_parts(b.ptr, b.len, b.cap)); }
    }
}

#[no_mangle]
pub extern "C" fn add(a: i64, b: i64) -> i64 { a.wrapping_add(b) }

#[no_mangle]
pub extern "C" fn add_wrong(a: i64, b: i64) -> i64 { a.wrapping_add(b).wrapping_add(1) }

#[no_mangle]
pub extern "C" fn fid(x: f64) -> f64 { x }

#[no_mangle]
pub extern "C" fn fzero(_x: f64) -> f64 { 0.0 }

#[no_mangle]
pub extern "C" fn flag(b: bool) -> bool { !b }

#[no_mangle]
pub extern "C" fn count_byte(p: *const u8, n: usize, t: i64) -> i64 {
    let s = unsafe { std::slice::from_raw_parts(p, n) };
    s.iter().filter(|&&b| b as i64 == t).count() as i64
}

#[no_mangle]
pub extern "C" fn doubled(p: *const u8, n: usize) -> RustBuf {
    let s = unsafe { std::slice::from_raw_parts(p, n) };
    let mut v = Vec::with_capacity(n * 2);
    v.extend_from_slice(s);
    v.extend_from_slice(s);
    make_buf(v)
}

#[no_mangle]
pub extern "C" fn boom(x: i64) -> i64 { if x == 42 { panic!("boom") } x }

#[no_mangle]
pub extern "C" fn spin(x: i64) -> i64 { if x == 7 { loop {} } x }
"""


@pytest.fixture(scope="module")
def dylib(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not RUSTC:
        pytest.skip("needs rustc")
    d = tmp_path_factory.mktemp("replay-fixture")
    rs = d / "fixture.rs"
    rs.write_text(FIXTURE_RS)
    out = d / "fixture.dylib"
    subprocess.run(
        [
            RUSTC,
            "--crate-type=cdylib",
            "-C",
            "panic=abort",
            "-C",
            "overflow-checks=on",
            "-O",
            "-o",
            str(out),
            str(rs),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return out


def sig(*kinds: str, ret: str) -> Signature:
    params = []
    for i, k in enumerate(kinds):
        if k in ("i64", "f64", "bool"):
            params.append(Param(name=f"a{i}", kind="scalar", scalar=k))
        else:
            params.append(Param(name=f"a{i}", kind="slice", text=(k == "str")))
    return Signature(params=tuple(params), ret=ret)


# --- replay against the fixture (toolchain-gated) ---------------------------


def test_correct_scalar_candidate_passes(dylib: Path) -> None:
    traces = [((1, 2), 3), ((-5, 7), 2), ((0, 0), 0)]
    assert replay_against_dylib(dylib, "add", sig("i64", "i64", ret="i64"), traces) == ""


def test_wrong_candidate_rejected_with_counterexample(dylib: Path) -> None:
    traces = [((1, 2), 3), ((-5, 7), 2)]
    verdict = replay_against_dylib(dylib, "add_wrong", sig("i64", "i64", ret="i64"), traces)
    assert "replay mismatch on trace #0" in verdict
    assert "(1, 2)" in verdict  # the counterexample input, for the escalation prompt


def test_float_compare_is_bitwise_with_nan_class(dylib: Path) -> None:
    good = [((1.5,), 1.5), ((float("nan"),), float("nan")), ((-0.0,), -0.0)]
    assert replay_against_dylib(dylib, "fid", sig("f64", ret="f64"), good) == ""
    # 0.0 is NOT -0.0 bitwise: an observable divergence a plain == would hide
    verdict = replay_against_dylib(dylib, "fzero", sig("f64", ret="f64"), [((-1.0,), -0.0)])
    assert "replay mismatch" in verdict


def test_bool_param_and_return(dylib: Path) -> None:
    traces = [((True,), False), ((False,), True)]
    assert replay_against_dylib(dylib, "flag", sig("bool", ret="bool"), traces) == ""


def test_str_slice_embedded_nul_and_unicode(dylib: Path) -> None:
    s = "héllo\x00wörld\U0001f389"  # embedded NUL + multibyte UTF-8
    expected = s.encode("utf-8").count(0x6C)  # count of b'l' — needs the FULL buffer
    traces = [((s, 0x6C), expected), (("", 0x6C), 0)]
    assert replay_against_dylib(dylib, "count_byte", sig("str", "i64", ret="i64"), traces) == ""


def test_rustbuf_return_roundtrip_growth_and_empty(dylib: Path) -> None:
    traces = [(("ab",), "abab"), (("",), ""), (("ß",), "ßß")]
    assert replay_against_dylib(dylib, "doubled", sig("str", ret="buf:str"), traces) == ""
    wrong = [(("ab",), "ab")]  # doubled("ab") is "abab"
    assert "replay mismatch" in replay_against_dylib(
        dylib, "doubled", sig("str", ret="buf:str"), wrong
    )


def test_panic_is_per_input_rejection_and_tail_survives(dylib: Path) -> None:
    traces = [((1,), 1), ((2,), 2), ((42,), 42), ((5,), 5), ((9,), 9)]
    verdict = replay_against_dylib(dylib, "boom", sig("i64", ret="i64"), traces)
    assert "replay crash" in verdict and "trace #2" in verdict and "(42)" in verdict
    # the respawn protocol: the crash consumed only its own index; the tail
    # after the aborting input was still evaluated (ok, ok, CRASH, ok, ok)
    results, hard, herr = _run_batches(dylib, "boom", ["i64"], "i64", False, traces, 30.0, None)
    assert herr is None
    assert set(hard) == {2} and hard[2].startswith("crash")
    assert {i for i, (ok, _) in results.items() if ok} == {0, 1, 3, 4}


def test_timeout_is_a_rejection(dylib: Path) -> None:
    traces = [((1,), 1), ((7,), 7)]
    verdict = replay_against_dylib(dylib, "spin", sig("i64", ret="i64"), traces, timeout=5.0)
    assert "replay timeout on trace #1" in verdict and "(7)" in verdict


def test_missing_symbol_is_harness_error_not_crash(dylib: Path) -> None:
    verdict = replay_against_dylib(dylib, "no_such_fn", sig("i64", ret="i64"), [((1,), 1)])
    assert verdict.startswith("replay: harness error")


# --- trace validation (pure Python, always runs) ----------------------------


def test_validation_rejects_out_of_i64_range() -> None:
    # ctypes silently wraps 2**63 -> -2**63: unchecked, a wrapped replay could
    # falsely PASS. The whole function is out of scope, not just the trace.
    err = validate_traces(sig("i64", ret="i64"), [((2**63,), 0)])
    assert "exceeds i64 range" in err
    err = validate_traces(sig("i64", ret="i64"), [((1,), 2**64)])
    assert "result" in err and "exceeds i64 range" in err


def test_validation_is_type_exact() -> None:
    assert "expected int, got bool" in validate_traces(sig("i64", ret="i64"), [((True,), 1)])
    assert "expected float, got int" in validate_traces(sig("f64", ret="f64"), [((1,), 1.0)])
    assert "expected bool, got int" in validate_traces(sig("bool", ret="bool"), [((1,), True)])


def test_validation_rejects_lone_surrogates_and_arity_mismatch() -> None:
    assert "lone surrogate" in validate_traces(sig("str", ret="i64"), [(("\ud800",), 0)])
    assert "has 2 args" in validate_traces(sig("i64", ret="i64"), [((1, 2), 3)])


def test_validation_accepts_the_good_cases() -> None:
    assert validate_traces(sig("i64", "f64", ret="i64"), [((1, 2.5), 3)]) == ""
    assert validate_traces(sig("bytes", ret="buf:bytes"), [((bytearray(b"ab"),), b"abab")]) == ""


def test_empty_traces_report_honestly() -> None:
    verdict = replay_against_dylib(Path("/nonexistent.dylib"), "f", sig("i64", ret="i64"), [])
    assert verdict == "replay: no captured traces to replay"
