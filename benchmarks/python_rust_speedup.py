"""Does a python->rust rewrite actually make Python *faster*? Self-contained:
build the same Rust escaper three ways — pure Python, Rust+ctypes (how `--apply`
verifies), and Rust+PyO3 (how `--apply --pyo3` ships) — and race them across
input sizes. The answer is a crossover set by the FFI *mechanism*, not by Rust.

    python benchmarks/python_rust_speedup.py     # ctypes needs rustc; PyO3 needs cargo

Headline (this machine): the ctypes boundary costs ~400ns/call, so the small
string leaves that dominate the eligible surface (docs/python-rust-surface.md)
come out *slower* through it; PyO3's boundary is ~0ns (as cheap as a Python
call), which flips those same cases to faster (20-char escape: 0.32x ctypes ->
1.38x PyO3) and wins big on real work (5KB escape: 5.6x). Verify with ctypes
(sound, only needs rustc); ship with PyO3 (fast).
"""

from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_RUST = """\
#[repr(C)]
pub struct RustBuf { ptr: *mut u8, len: usize, cap: usize }
fn mk(v: Vec<u8>) -> RustBuf { let mut v = std::mem::ManuallyDrop::new(v);
    RustBuf { ptr: v.as_mut_ptr(), len: v.len(), cap: v.capacity() } }
#[no_mangle] pub extern "C" fn cgir_buf_free(b: RustBuf) {
    if !b.ptr.is_null() { unsafe { drop(Vec::from_raw_parts(b.ptr, b.len, b.cap)); } } }
#[no_mangle] pub extern "C" fn escape(p: *const u8, n: usize) -> RustBuf {
    let s = unsafe { std::slice::from_raw_parts(p, n) };
    let mut out: Vec<u8> = Vec::with_capacity(n);
    for &b in s { match b {
        b'&' => out.extend_from_slice(b"&amp;"), b'<' => out.extend_from_slice(b"&lt;"),
        b'>' => out.extend_from_slice(b"&gt;"), b'\\'' => out.extend_from_slice(b"&#39;"),
        b'"' => out.extend_from_slice(b"&#34;"), _ => out.push(b) } }
    mk(out) }
"""


def py_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("'", "&#39;")
        .replace('"', "&#34;")
    )


def main() -> None:
    if not shutil.which("rustc"):
        print("needs rustc")
        return
    d = Path(tempfile.mkdtemp())
    rs, lib = d / "e.rs", d / ("e.dylib" if sys.platform == "darwin" else "e.so")
    rs.write_text(_RUST)
    subprocess.run(["rustc", "--crate-type=cdylib", "-O", "-o", str(lib), str(rs)], check=True)

    class Buf(ctypes.Structure):
        _fields_ = [("ptr", ctypes.c_void_p), ("len", ctypes.c_size_t), ("cap", ctypes.c_size_t)]

    dll = ctypes.CDLL(str(lib))
    dll.escape.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
    dll.escape.restype = Buf
    dll.cgir_buf_free.argtypes = [Buf]

    def rs_escape(s: str) -> str:
        b = s.encode("utf-8")
        r = dll.escape(b, len(b))
        try:
            return ctypes.string_at(r.ptr, r.len).decode("utf-8") if r.len else ""
        finally:
            dll.cgir_buf_free(r)

    rs_pyo3 = _build_pyo3_escape(d)  # None if no cargo

    inputs = {
        "empty": ("", 300000),
        "20ch no-special": ("hello world foobar!!", 300000),
        "20ch w/ specials": ('<a href="x">&y</a>', 300000),
        "200ch mixed": (("<p>x & y</p> " * 15)[:200], 200000),
        "5KB no-special": ("x" * 5000, 40000),
        "5KB w/ specials": (("<b>a&b</b> " * 500)[:5000], 40000),
    }
    for _name, (s, _) in inputs.items():
        assert py_escape(s) == rs_escape(s)
        if rs_pyo3:
            assert py_escape(s) == rs_pyo3(s)

    def ns(fn, arg, n):  # type: ignore[no-untyped-def]
        t = time.perf_counter()
        for _ in range(n):
            fn(arg)
        return (time.perf_counter() - t) / n * 1e9

    hdr = f"{'input':18s} {'Python':>10s} {'ctypes':>10s} {'PyO3':>10s}  {'ctypes/PyO3 vs Python':>22s}"
    print(
        hdr
        if rs_pyo3
        else f"{'input':18s} {'Python(ns)':>12s} {'Rust+ctypes':>12s} {'speedup':>9s}"
    )
    for name, (s, n) in inputs.items():
        p, r = ns(py_escape, s, n), ns(rs_escape, s, n)
        if rs_pyo3:
            q = ns(rs_pyo3, s, n)
            print(f"{name:18s} {p:10.0f} {r:10.0f} {q:10.0f}  {p / r:9.2f}x / {p / q:.2f}x")
        else:
            print(f"{name:18s} {p:12.0f} {r:12.0f} {p / r:8.2f}x")
    ctypes_tax = ns(rs_escape, "", 300000) - ns(py_escape, "", 300000)
    print(f"\nfixed ctypes tax ~= {ctypes_tax:.0f} ns/call")
    if rs_pyo3:
        pyo3_tax = ns(rs_pyo3, "", 300000) - ns(py_escape, "", 300000)
        print(
            f"fixed PyO3   tax ~= {pyo3_tax:.0f} ns/call  (~{ctypes_tax / max(pyo3_tax, 1):.0f}x cheaper)"
        )


_PYO3_RS = """\
use pyo3::prelude::*;
#[pyfunction]
fn escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() { match c {
        '&' => out.push_str("&amp;"), '<' => out.push_str("&lt;"), '>' => out.push_str("&gt;"),
        '\\'' => out.push_str("&#39;"), '"' => out.push_str("&#34;"), _ => out.push(c) } }
    out }
#[pymodule]
fn esc(m: &Bound<'_, PyModule>) -> PyResult<()> { m.add_function(wrap_pyfunction!(escape, m)?)?; Ok(()) }
"""


def _build_pyo3_escape(d: Path):  # type: ignore[no-untyped-def]
    """Build the same escaper as a native PyO3 extension; None if no cargo."""
    if not shutil.which("cargo"):
        return None
    import importlib.util

    crate = d / "pyo3esc"
    (crate / "src").mkdir(parents=True, exist_ok=True)
    (crate / ".cargo").mkdir(exist_ok=True)
    (crate / "Cargo.toml").write_text(
        '[package]\nname = "esc"\nversion = "0.0.0"\nedition = "2021"\n'
        '[lib]\nname = "esc"\ncrate-type = ["cdylib"]\n'
        '[dependencies]\npyo3 = { version = "0.22", features = ["extension-module", "abi3-py38"] }\n'
    )
    (crate / ".cargo" / "config.toml").write_text(
        '[target.aarch64-apple-darwin]\nrustflags = ["-C","link-arg=-undefined","-C","link-arg=dynamic_lookup"]\n'
        '[target.x86_64-apple-darwin]\nrustflags = ["-C","link-arg=-undefined","-C","link-arg=dynamic_lookup"]\n'
    )
    (crate / "src" / "lib.rs").write_text(_PYO3_RS)
    if subprocess.run(["cargo", "build", "--release"], cwd=crate, capture_output=True).returncode:
        return None
    built = next(iter((crate / "target" / "release").glob("libesc.*dylib")), None) or next(
        iter((crate / "target" / "release").glob("libesc.so")), None
    )
    so = d / "esc.abi3.so"
    shutil.copy(built, so)  # type: ignore[arg-type]
    spec = importlib.util.spec_from_file_location("esc", so)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.escape


if __name__ == "__main__":
    main()
