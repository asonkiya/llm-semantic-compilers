"""Does a python->rust rewrite actually make Python *faster*? Self-contained:
compile a Rust escaper to a cdylib, wrap it the way ``--apply`` does (ctypes,
(ptr,len) in, RustBuf out), and race it against the equivalent pure-Python
across input sizes. The answer is a crossover, and it is set by the FFI
mechanism, not by Rust.

    python benchmarks/python_rust_speedup.py     # needs rustc

Headline (this machine): ctypes costs ~390ns/call fixed, so the small string
leaves that dominate the eligible surface (docs/python-rust-surface.md) are
*slower* via Rust+ctypes; Rust only wins once the per-call compute clears the
tax (large inputs with real work). The verification pipeline uses ctypes for
simplicity; a production emit target (PyO3, ~10-50ns/call) would move the
crossover down by ~10x. Verify with ctypes; ship with PyO3.
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

    def ns(fn, arg, n):  # type: ignore[no-untyped-def]
        t = time.perf_counter()
        for _ in range(n):
            fn(arg)
        return (time.perf_counter() - t) / n * 1e9

    print(f"{'input':18s} {'Python(ns)':>12s} {'Rust+ctypes':>12s} {'speedup':>9s}")
    for name, (s, n) in inputs.items():
        p, r = ns(py_escape, s, n), ns(rs_escape, s, n)
        print(f"{name:18s} {p:12.0f} {r:12.0f} {p / r:8.2f}x")
    tax = ns(rs_escape, "", 300000) - ns(py_escape, "", 300000)
    print(f"\nfixed ctypes tax ~= {tax:.0f} ns/call (empty-input delta)")


if __name__ == "__main__":
    main()
