"""PyO3 apply target — ship the verified Rust as a native Python extension.

`--apply` verifies with ctypes (simple, sound) but that FFI costs ~390 ns/call,
so the small leaves it can rewrite come out *slower* (docs/python-rust-surface.md).
This builds the same verified winners into a native PyO3 extension instead
(~50 ns/call, ~7x less overhead): the exact ``extern "C"`` functions that passed
replay are kept verbatim, and a thin generated ``#[pyfunction]`` shim marshals
Python types over them. Verify with ctypes; ship with PyO3 — same bytes, faster
boundary.

Needs ``cargo`` on PATH (and network on the first build to fetch pyo3); the
ctypes path needs only ``rustc``, so this is opt-in (``--pyo3``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from cgir.ffi.sources.python import PyEntry
from cgir.ffi.targets.rust import assemble_python_winners

_PYO3_VERSION = "0.22"
_CARGO_CONFIG = """\
[target.aarch64-apple-darwin]
rustflags = ["-C", "link-arg=-undefined", "-C", "link-arg=dynamic_lookup"]
[target.x86_64-apple-darwin]
rustflags = ["-C", "link-arg=-undefined", "-C", "link-arg=dynamic_lookup"]
"""


def _rust_scalar(name: str) -> str:
    return name  # "i64"/"f64"/"bool" are Rust types verbatim


def render_pyo3_shim(e: PyEntry) -> str:
    """A ``#[pyfunction]`` that marshals Python types onto the verified
    ``extern "C"`` winner and calls it (named ``py_<sym>``, exposed to Python as
    ``<sym>``). Slice params take ``&str``/``&[u8]``; RustBuf returns are copied
    out and freed. Reads RustBuf fields directly — the shim shares the crate
    module with the (private-field) prelude."""
    py_params: list[str] = []
    c_args: list[str] = []
    ret = e.sig.ret
    bytes_ret = ret == "buf:bytes"
    if bytes_ret:
        py_params.append("py: Python<'py>")
    for p in e.sig.params:
        if p.kind == "scalar":
            assert p.scalar is not None
            py_params.append(f"{p.name}: {_rust_scalar(p.scalar)}")
            c_args.append(p.name)
        else:  # slice
            py_params.append(f"{p.name}: {'&str' if p.text else '&[u8]'}")
            c_args += [f"{p.name}.as_ptr()", f"{p.name}.len()"]
    call = f"{e.symbol}({', '.join(c_args)})"
    lifetime = "<'py>" if bytes_ret else ""
    head = (
        f'#[pyfunction]\n#[pyo3(name = "{e.symbol}")]\n'
        f"fn py_{e.symbol}{lifetime}({', '.join(py_params)})"
    )
    if not ret.startswith("buf:"):
        return f"{head} -> {_rust_scalar(ret)} {{ unsafe {{ {call} }} }}"
    take = (
        f"let __b = {call};\n"
        f"        let __v = if __b.len == 0 {{ Vec::new() }} else "
        f"{{ std::slice::from_raw_parts(__b.ptr as *const u8, __b.len).to_vec() }};\n"
        f"        cgir_buf_free(__b);"
    )
    if bytes_ret:
        return (
            f"{head} -> Bound<'py, PyBytes> {{ unsafe {{\n"
            f"        {take}\n        PyBytes::new_bound(py, &__v)\n    }} }}"
        )
    return (
        f"{head} -> String {{ unsafe {{\n"
        f"        {take}\n        String::from_utf8_lossy(&__v).into_owned()\n    }} }}"
    )


def render_pyo3_lib(winners: dict[str, str], entries: list[PyEntry], module: str) -> str:
    """The full ``lib.rs``: the verified winners (prelude deduped) verbatim, the
    per-winner shims, and the ``#[pymodule]``."""
    shims = "\n\n".join(render_pyo3_shim(e) for e in entries)
    adds = "\n".join(f"    m.add_function(wrap_pyfunction!(py_{e.symbol}, m)?)?;" for e in entries)
    return f"""\
#![allow(dead_code, non_snake_case)]
use pyo3::prelude::*;
use pyo3::types::PyBytes;

// --- verified extern "C" winners (replay-passed, verbatim) ------------------
{assemble_python_winners(winners)}

// --- generated PyO3 shims over them -----------------------------------------
{shims}

#[pymodule]
fn {module}(m: &Bound<'_, PyModule>) -> PyResult<()> {{
{adds}
    Ok(())
}}
"""


def build_pyo3_extension(
    winners: dict[str, str], entries: list[PyEntry], module: str, workdir: Path
) -> tuple[Path | None, str]:
    """Build the winners into a native ``{module}`` extension. Returns
    (path-to-.so-or-.dylib, "") or (None, cargo error)."""
    crate = workdir / "pyo3_ext"
    (crate / "src").mkdir(parents=True, exist_ok=True)
    (crate / ".cargo").mkdir(exist_ok=True)
    (crate / "Cargo.toml").write_text(
        f'[package]\nname = "{module}"\nversion = "0.0.0"\nedition = "2021"\n\n'
        f'[lib]\nname = "{module}"\ncrate-type = ["cdylib"]\n\n'
        f'[dependencies]\npyo3 = {{ version = "{_PYO3_VERSION}", '
        f'features = ["extension-module", "abi3-py38"] }}\n'
    )
    (crate / ".cargo" / "config.toml").write_text(_CARGO_CONFIG)
    (crate / "src" / "lib.rs").write_text(render_pyo3_lib(winners, entries, module))
    proc = subprocess.run(
        ["cargo", "build", "--release"],
        cwd=crate,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        return None, "\n".join(proc.stderr.splitlines()[-25:])
    rel = crate / "target" / "release"
    for suffix in (".dylib", ".so"):
        lib = rel / f"lib{module}{suffix}"
        if lib.exists():
            return lib, ""
    return None, f"cargo succeeded but no lib{module}.(dylib|so) in {rel}"


def extension_filename(module: str) -> str:
    """The importable name for the built extension (abi3, version-independent)."""
    return f"{module}.abi3.so"
