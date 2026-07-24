"""Python -> Rust cross-language regeneration — the ``cgir rewrite --lang
python-rust`` engine, assembled from the FFI core (docs/design-ffi-pipeline.md
§6). The proving second instance of the language-neutral pipeline.

Given a cgir index + repo and captured traces from the repo's own test suite,
regenerate pure, fully-typed leaf Python functions (params + return in
``int | float | bool | str | bytes``) as ``#[no_mangle] extern "C"`` Rust,
compile each to a cdylib, and verify by replaying the recorded ``(args,
result)`` pairs against it.

    worklist (pure typed leaves, from the index)         [ffi.sources.python]
      -> cheap-model Rust candidate (source + exact FFI signature + rules)
      -> rustc --crate-type=cdylib -C panic=abort -C overflow-checks=on
                                                          [ffi.targets.rust]
      -> ABI check: the symbol is exported                [ffi.driver]
      -> replay the captured traces against it            [ffi.replay_ffi]
      -> one escalation carrying the compiler error or counterexample

Rides the shared :func:`cgir.rewrite.run_search_loop`. Unlike C->Rust there is
no isolated fuzz differential (there is no compiled "original" to fuzz — the
Python original isn't a dylib); the recorded traces are the behavioral oracle,
so the verified property is agreement on the recorded, non-raising inputs.
Verification builds carry ``overflow-checks=on`` so a silent i64 overflow in
the candidate becomes a detectable abort (a rejection) rather than a wrong
answer only some trace catches; ``panic=abort`` makes every panic an abort the
replay harness isolates per-input.

Toolchain: ``rustc`` on PATH; ``pytest`` (via the target repo) to capture.
Network only via the injected sampler (``--live``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from cgir.ffi.driver import exported_symbols
from cgir.ffi.replay_ffi import Trace, replay_against_dylib, validate_traces
from cgir.ffi.sources.python import PyEntry, python_rust_worklist
from cgir.ffi.targets.rust import (
    RUSTBUF_PRELUDE,
    assemble_python_winners,
    rust_signature_ir,
    try_rustc,
)
from cgir.rewrite import Sampler, run_search_loop

# The importable name of the emitted ctypes wrapper module and its native lib.
_WRAPPER_MODULE = "_cgir_rs"
_LIB_STEM = "_cgir_rs_lib"

# ctypes scalar for a canonical FFI scalar name.
_CTYPES_SCALAR = {"i64": "c_int64", "f64": "c_double", "bool": "c_bool"}

# Verification-build flags: overflow-checks turns a silent i64 wrap into an
# abort (a rejection); panic=abort makes every panic an abort the harness
# isolates. Both were fixed by the M2 marshalling research.
_RUSTC_FLAGS = ["-C", "panic=abort", "-C", "overflow-checks=on"]


def _expand_self_traces(traces: list[Trace], sig: Any) -> list[Trace]:
    """Turn a value-self method's traces into flat ones: the captured ``self``
    (arg 0) becomes its read fields, matching the ``from_self`` params (which
    lead ``sig.params``). A trace whose ``self`` is missing a field is dropped."""
    from_self = [p for p in sig.params if p.from_self]
    out: list[Trace] = []
    for args, result in traces:
        if not args:
            continue
        self_obj = args[0]
        try:
            fields = tuple(getattr(self_obj, p.name) for p in from_self)
        except AttributeError:
            continue
        out.append((fields + tuple(args[1:]), result))
    return out


def _dedup_traces(traces: list[Trace]) -> list[Trace]:
    """Distinct-by-argument traces (first result kept). Pure functions map an
    input to one output, so the distinct inputs are the evidence; the dups a hot
    leaf accumulates just slow replay down. ``bytearray`` args are keyed by their
    bytes (they're unhashable)."""
    seen: set[Any] = set()
    out: list[Trace] = []
    for args, result in traces:
        key = tuple(bytes(a) if isinstance(a, bytearray) else a for a in args)
        try:
            if key in seen:
                continue
            seen.add(key)
        except TypeError:  # an unhashable arg slipped through — keep it, don't dedup
            pass
        out.append((args, result))
    return out


def build_python_rust_prompt(e: PyEntry) -> str:
    sig = e.sig
    has_slice = any(p.kind == "slice" for p in sig.params)
    ret_buf = sig.ret.startswith("buf:")
    self_rule = ""
    if sig.self_param:
        fields = [p.name for p in sig.params if p.from_self]
        mapping = ", ".join(f"`self.{f}` -> parameter `{f}`" for f in fields)
        self_rule = (
            f"\n- This is a method being rewritten as a FREE function of its fields. "
            f"There is no `self`: replace each field read with its parameter — {mapping}. "
            f"Any other parameter is passed as-is."
        )
    slice_rule = ""
    if has_slice:
        slice_rule = (
            "\n- Each `str`/`bytes` parameter arrives as a `(ptr: *const u8, len: usize)` "
            "pair. Rebuild the slice with `unsafe { std::slice::from_raw_parts(ptr, len) }` "
            "(handle `len == 0` without dereferencing); a `str` slice is valid UTF-8, so "
            "`std::str::from_utf8(...).unwrap()` is safe *only* because the caller guarantees "
            "it — prefer `if let Ok(s) = std::str::from_utf8(...)`."
        )
    prelude_rule = out_rule = ""
    if ret_buf:
        prelude_rule = (
            "\n- The function returns a string/bytes value. Include this prelude EXACTLY "
            "as written (do not modify it), and return your output via `cgir_make_buf(vec)`:\n"
            f"```rust\n{RUSTBUF_PRELUDE}```"
        )
        out_rule = (
            "- Output the prelude above, then your one function item — no markdown fences, "
            "no `use` statements outside the items, no prose."
        )
    else:
        out_rule = (
            "- Output ONLY that one function item, no markdown fences, no `use` statements, "
            "no extra items, no comments about the translation."
        )
    return f"""Translate this pure Python function into an equivalent Rust function.

```python
{e.source}```

It is called through the C ABI. The EXACT item you must produce is:

{rust_signature_ir(e.symbol, sig)} {{
    ...
}}

Rules:
{out_rule}
- Match the Python function's result EXACTLY on every input.
- Python `int` is unbounded. The recorded inputs fit in `i64`, but an
  intermediate computation may overflow — use `i128` or `checked_*`/`wrapping_*`
  DELIBERATELY to reproduce Python's exact arithmetic. An overflow will be
  rejected, so never let one happen silently.
- Never panic (no `unwrap` on a fallible path, no index out of bounds, no
  divide-by-zero the Python does not have). A panic is a rejection.
- Deterministic: no I/O, no globals, no time/random.{self_rule}{slice_rule}{prelude_rule}"""


def run_python_rust(
    index_dir: Path,
    repo: Path,
    *,
    sampler: Sampler,
    traces: dict[str, list[Trace]],
    query: str = "kind:pure",
    k: int = 3,
    min_traces: int = 3,
    budget_usd: float | None = None,
    ledger_path: Path | None = None,
    apply: bool = False,
    pyo3: bool = False,
    log: Any = lambda _: None,
) -> dict[str, Any]:
    """Regenerate ``repo``'s eligible pure functions in Rust, replay-verified
    against ``traces`` (keyed by component id). Rides
    :func:`cgir.rewrite.run_search_loop`; report shape matches ``run_c_rust``.

    With ``apply`` the verified winners are assembled into one cdylib, a ctypes
    wrapper module is emitted into ``repo``, each rewritten Python body is
    spliced with a thin wrapper delegating to it, and the final gate (rescan +
    the repo's full test suite) runs — the authoritative check that the repo is
    behaviorally unchanged with Rust inside."""
    workdir = Path(tempfile.mkdtemp(prefix="cgir-pyrust-"))
    entries, excluded = python_rust_worklist(index_dir, repo, query)
    by_id = {e.component_id: e for e in entries}

    # A function is verifiable only with enough valid recorded inputs; drop the
    # rest into `excluded` with a specific reason (the verified property is
    # agreement on the recorded inputs, so weak evidence is no evidence).
    # Dedup by argument tuple first: a hot leaf (e.g. markupsafe's escape) can be
    # called 80k times on ~26 distinct inputs — for a pure function the distinct
    # inputs are the real evidence, and replaying the dups just burns time.
    prepared: dict[str, list[Trace]] = {}
    verifiable: list[PyEntry] = []
    for e in entries:
        raw = traces.get(e.component_id, [])
        if e.sig.self_param:
            raw = _expand_self_traces(raw, e.sig)
        t = _dedup_traces(raw)
        if not t:
            excluded.append((e.component_id, "no captured traces"))
            continue
        reason = validate_traces(e.sig, t)
        if reason:
            excluded.append((e.component_id, reason))
            continue
        if len(t) < min_traces:
            excluded.append(
                (e.component_id, f"only {len(t)} distinct inputs (< min-traces {min_traces})")
            )
            continue
        prepared[e.component_id] = t
        verifiable.append(e)

    counter = {"n": 0}

    def make_prompt(e: PyEntry) -> str:
        return build_python_rust_prompt(e)

    def evaluate(e: PyEntry, cand: str) -> tuple[str, str, dict[str, Any]]:
        counter["n"] += 1
        dylib, err = try_rustc(
            cand, workdir, f"{e.symbol}_{counter['n']}", extra_flags=_RUSTC_FLAGS
        )
        if dylib is None:
            return "rustc", err, {}
        if e.symbol not in exported_symbols(dylib, [e.symbol]):
            return "abi", f"candidate does not export `{e.symbol}` as a no_mangle extern C fn", {}
        verdict = replay_against_dylib(dylib, e.symbol, e.sig, prepared[e.component_id])
        if verdict:
            return "replay", verdict, {}
        return "ok", "", {"regenerated_as": f"rust:{e.symbol}", "verify": "replay"}

    loop = run_search_loop(
        verifiable,
        build_prompt=make_prompt,
        evaluate=evaluate,
        sampler=sampler,
        id_of=lambda e: e.component_id,
        k=k,
        budget_usd=budget_usd,
        ledger_path=ledger_path,
        report_meta={"lang": "python-rust", "repo": str(repo), "min_traces": min_traces},
        log=log,
    )
    outcomes = loop["outcomes"]
    stage_kills: dict[str, int] = {}
    for o in outcomes:
        for a in o["attempts"]:
            if a["stage"] != "ok":
                stage_kills[a["stage"]] = stage_kills.get(a["stage"], 0) + 1
    loop["excluded"] = [{"id": i, "reason": r} for i, r in excluded]
    loop["stage_kills"] = stage_kills
    loop["results"] = outcomes
    if apply:
        loop["final_gate"] = apply_python_rust_winners(
            index_dir, repo, loop, by_id, workdir, pyo3=pyo3
        )
        if ledger_path is not None:
            ledger_path.write_text(json.dumps(loop, indent=2) + "\n")
    return loop


def _lib_filename() -> str:
    return _LIB_STEM + (".dylib" if sys.platform == "darwin" else ".so")


def render_python_wrapper(e: PyEntry) -> str:
    """The thin Python body that replaces the original function: same name and
    parameter names (so callers are unaffected), delegating to the extension via
    the emitted wrapper module. For a value-self method the ``def`` keeps its
    original params (``self``, ...) and the call reads the ``from_self`` params
    off ``self``. Annotations are dropped — a deliberate, gate-downgraded drift
    on exactly the rewritten set."""
    sig = e.sig
    if sig.self_param:
        def_params = ", ".join([sig.self_param] + [p.name for p in sig.params if not p.from_self])
        call_args = ", ".join(f"self.{p.name}" if p.from_self else p.name for p in sig.params)
    else:
        def_params = call_args = ", ".join(p.name for p in sig.params)
    return (
        f"def {e.symbol}({def_params}):\n"
        f"    from {_WRAPPER_MODULE} import {e.symbol} as _rs\n"
        f"    return _rs({call_args})"
    )


def render_wrapper_module(winners: list[PyEntry], libname: str) -> str:
    """The generated ctypes wrapper: loads the cdylib once, sets argtypes /
    restype per FFI signature, and marshals each call (slice params -> (bytes,
    len); RustBuf returns -> ``string_at`` + ``cgir_buf_free``)."""
    needs_free = any(e.sig.ret.startswith("buf:") for e in winners)
    out = [
        '"""Generated by `cgir rewrite --lang python-rust --apply` — Rust',
        "implementations loaded over the C ABI. Do not edit by hand.",
        '"""',
        "",
        "import ctypes as _c",
        "import os as _os",
        "",
        f"_LIB = _c.CDLL(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), {libname!r}))",
        "",
        "",
        "class _RustBuf(_c.Structure):",
        '    _fields_ = [("ptr", _c.c_void_p), ("len", _c.c_size_t), ("cap", _c.c_size_t)]',
        "",
    ]
    if needs_free:
        out += [
            "",
            "_free = _LIB.cgir_buf_free",
            "_free.argtypes = [_RustBuf]",
            "_free.restype = None",
            "",
        ]
    for e in winners:
        argtypes: list[str] = []
        for p in e.sig.params:
            if p.kind == "scalar":
                assert p.scalar is not None
                argtypes.append(f"_c.{_CTYPES_SCALAR[p.scalar]}")
            else:
                argtypes += ["_c.c_char_p", "_c.c_size_t"]
        restype = "_RustBuf" if e.sig.ret.startswith("buf:") else f"_c.{_CTYPES_SCALAR[e.sig.ret]}"
        fn = f"_LIB.{e.symbol}"
        out += [
            "",
            f"# --- {e.symbol} ---",
            f"{fn}.argtypes = [{', '.join(argtypes)}]",
            f"{fn}.restype = {restype}",
            f"def {e.symbol}({', '.join(p.name for p in e.sig.params)}):",
        ]
        call_args: list[str] = []
        for i, p in enumerate(e.sig.params):
            if p.kind == "scalar":
                call_args.append(p.name)
            else:
                enc = f'{p.name}.encode("utf-8")' if p.text else f"bytes({p.name})"
                out.append(f"    _a{i} = {enc}")
                call_args += [f"_a{i}", f"len(_a{i})"]
        if e.sig.ret.startswith("buf:"):
            out.append(f"    _r = {fn}({', '.join(call_args)})")
            decode = '.decode("utf-8")' if e.sig.ret == "buf:str" else ""
            empty = '""' if e.sig.ret == "buf:str" else "b''"
            out += [
                "    try:",
                f"        return _c.string_at(_r.ptr, _r.len){decode} if _r.len else {empty}",
                "    finally:",
                "        _free(_r)",
            ]
        else:
            out.append(f"    return {fn}({', '.join(call_args)})")
    return "\n".join(out) + "\n"


def apply_python_rust_winners(
    index_dir: Path,
    repo: Path,
    report: dict[str, Any],
    by_id: dict[str, PyEntry],
    workdir: Path,
    pyo3: bool = False,
) -> dict[str, Any]:
    """Assemble the verified winners, emit the wrapper module (ctypes ``.py`` by
    default, or a native PyO3 ``.so`` with ``pyo3=True``), splice each rewritten
    Python body with a delegating wrapper, then run the final gate: rescan for
    hard contract drift *outside* the rewritten set (drift on the rewritten
    functions — lost annotations, the new call — is expected and downgraded) and
    the repo's full test suite (the authoritative check).

    Both emit modes produce the same importable ``_cgir_rs`` and splice the same
    wrappers; PyO3 ships the same verified Rust behind a ~7x-cheaper boundary."""
    from cgir.report.diff import compute_diff
    from cgir.verify import _find_node, _hard_drift, _splice

    winners: dict[str, str] = {}
    spliced: list[tuple[Any, str, PyEntry]] = []
    for o in report["outcomes"]:
        if o["status"] != "solved":
            continue
        e = by_id.get(o["component_id"])
        node = _find_node(index_dir, o["component_id"])
        if e is None or node is None or node.path is None:
            continue
        winners[e.symbol] = o["attempts"][-1]["candidate"]
        spliced.append((node, o["attempts"][-1]["candidate"], e))
    if not winners:
        return {"applied": 0, "note": "no winners to apply"}
    entries = [e for _, _, e in spliced]

    # 1-2. emit the extension (native PyO3) or wrapper module + cdylib (ctypes).
    if pyo3:
        from cgir.ffi.targets.pyo3 import build_pyo3_extension, extension_filename

        lib, err = build_pyo3_extension(winners, entries, _WRAPPER_MODULE, workdir)
        if lib is None:
            return {"applied": 0, "error": f"PyO3 extension build failed:\n{err}"}
        artifact = extension_filename(_WRAPPER_MODULE)
        shutil.copy(lib, repo / artifact)
    else:
        lib_dylib, err = try_rustc(
            assemble_python_winners(winners),
            workdir,
            "apply",
            extra_flags=["-C", "panic=abort", "-C", "overflow-checks=on"],
        )
        if lib_dylib is None:
            return {"applied": 0, "error": f"assembled cdylib failed to build:\n{err}"}
        artifact = _lib_filename()
        shutil.copy(lib_dylib, repo / artifact)
        (repo / f"{_WRAPPER_MODULE}.py").write_text(render_wrapper_module(entries, artifact))

    # 3. splice thin wrappers (descending span order within a file so earlier
    # splices don't shift later spans). Same wrapper for both emit modes.
    for node, _cand, e in sorted(spliced, key=lambda t: (t[0].path, -(t[0].start_line or 0))):
        _splice(repo / node.path, node.start_line, node.end_line, render_python_wrapper(e))

    # 4. final gate.
    rewritten = {cid for cid in (o["component_id"] for o in report["outcomes"])}
    new_index = Path(tempfile.mkdtemp(prefix="cgir-pyrust-gate-")) / "idx"
    from cgir.export.json_export import read_specs
    from cgir.pipeline import scan_repo

    scan_repo(repo, out=new_index)
    diff = compute_diff(read_specs(index_dir), read_specs(new_index))
    dirty = [c["id"] for c in diff["changed"] if _hard_drift(c) and c["id"] not in rewritten]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    return {
        "applied": len(winners),
        "emit": "pyo3" if pyo3 else "ctypes",
        "artifact": artifact,
        "contract_clean_outside_rewritten": not dirty,
        "hard_drift_outside_rewritten": dirty,
        "tests_ok": proc.returncode == 0,
        "tests_output": proc.stdout[-2000:] if proc.returncode != 0 else "",
    }
