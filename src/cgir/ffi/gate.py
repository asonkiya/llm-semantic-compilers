"""The whole-program gate — the FFI core's authoritative acceptance test.

Recipe-driven and workload-based: build the real program with one candidate
linked in (``build_cmd`` with ``{source}``/``{lib}``/``{out}`` placeholders),
run the real workload (``run_cmd`` with ``{out}``, optional stdin), and keep
the candidate only if the output is byte-identical to stock. This is the
layer that decides gate-only functions (struct pointers, unfuzzable
preconditions) and catches hidden-runtime-state divergence no isolated check
can model.

The assembly steps currently bind the C-source/Rust-target pair (patching a
C TU, building a Rust staticlib); a second compiled pair generalizes these to
injected assemblers.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from cgir.ffi.ir import CEntry
from cgir.ffi.sources.c import _patch_source
from cgir.ffi.targets.rust import _build_rust_staticlib, extern_block


def _gate_build_run(
    c_source: Path,
    subset: dict[str, str],
    entries: list[CEntry],
    build_cmd: str,
    run_cmd: str,
    run_input: bytes,
    workdir: Path,
) -> tuple[int | None, str, str]:
    """Build the real program with ``subset`` replaced by Rust and run it.
    Returns (returncode|None-if-build-failed, stdout, build_stderr)."""
    d = Path(tempfile.mkdtemp(dir=workdir))
    by_name = {e.name: e for e in entries}
    still_c = {
        c
        for w in subset
        for c in by_name.get(w, CEntry("", "", "", [], "")).callees
        if c in by_name and c not in subset
    }
    lib = _build_rust_staticlib(subset, d, extern_block([by_name[c] for c in sorted(still_c)]))
    patched = _patch_source(c_source, sorted(subset), d, also_export=still_c)
    prog = d / "prog"
    build = build_cmd.format(source=str(patched), lib=str(lib), out=str(prog))
    b = subprocess.run(build, shell=True, capture_output=True, text=True, timeout=600)
    if b.returncode != 0:
        return None, "", b.stderr[-1500:]
    run = run_cmd.format(out=str(prog))
    r = subprocess.run(run, shell=True, input=run_input, capture_output=True, timeout=180)
    return r.returncode, r.stdout.decode("utf-8", "replace"), ""


def whole_program_gate(
    c_source: Path,
    winners: dict[str, str],
    entries: list[CEntry],
    build_cmd: str,
    run_cmd: str,
    run_input: bytes = b"",
    workdir: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    """The authoritative acceptance test: replay the real *workload*, not a
    dead pointer. For each winner, build the program with only that function
    replaced by Rust (``build_cmd`` — placeholders ``{source}`` patched C,
    ``{lib}`` Rust staticlib, ``{out}`` binary) and run it (``run_cmd`` —
    ``{out}``, stdin ``run_input``); keep it only if the output is
    byte-identical to stock and it did not crash. Catches functions whose
    contract depends on hidden runtime state (allocation metadata, etc.) that
    the isolated differential can't model — with no name heuristic. Returns
    (verified, {rejected: reason})."""
    wd = workdir or Path(tempfile.mkdtemp(prefix="cgir-gate-"))
    stock_rc, stock_out, err = _gate_build_run(
        c_source, {}, entries, build_cmd, run_cmd, run_input, wd
    )
    if stock_rc != 0:
        raise RuntimeError(f"gate stock build/run failed (rc={stock_rc}):\n{err}")
    verified: list[str] = []
    rejected: dict[str, str] = {}
    for name in sorted(winners):
        rc, out, _ = _gate_build_run(
            c_source, {name: winners[name]}, entries, build_cmd, run_cmd, run_input, wd
        )
        if rc is None:
            rejected[name] = "build_fail"
        elif rc != 0:
            rejected[name] = f"crash(rc={rc})"
        elif out != stock_out:
            rejected[name] = "diverged"
        else:
            verified.append(name)
    return verified, rejected
