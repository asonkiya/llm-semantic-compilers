"""C -> Rust cross-language regeneration — the ``cgir rewrite --lang c-rust``
engine (vision-rewrite.md rung 4), assembled from the language-neutral FFI
core (:mod:`cgir.ffi`, docs/design-ffi-pipeline.md).

Given a cgir index and a single compilable C translation unit (an
amalgamation like ``sqlite3.c``, or any one ``.c`` whose worklist symbols it
defines), regenerate its pure functions in Rust and verify each one
mechanically. Leaf-only by default; with ``include_nonleaf`` it also
rewrites functions that call other worklist functions, processed
callees-first — a rewritten Rust caller reaches its callees as ``extern
"C"`` symbols (the original C during verification via the oracle +
RTLD_GLOBAL; the rewritten Rust after link-back). This is the one-pass path:
a whole dependency subgraph rewritten and assembled with all-Rust internal
calls.

    worklist (pure functions with scalar / byte-pointer ABI, from the index)
      -> cheap-model Rust candidate (source + compiler-probed context
         + extern "C" decls for in-repo callees)
      -> rustc                       (compile filter)
      -> cgir Rust-adapter scan       (cross-language contract: pure + arity)
      -> differential vs the compiled C original
         (a fault-trapping C driver; orig-faulting inputs are out-of-contract
          and skipped; pointer params fuzzed with dual buffers + mutation
          compare)
      -> one escalation carrying the compiler error or counterexample

Rides the shared :func:`cgir.rewrite.run_search_loop`, so it inherits the
same k-sampling / escalation / ledger / budget machinery as the Python
``cgir rewrite`` path. The isolated-differential set is scalar and
char*/byte-buffer leaves. With ``structs`` it also rewrites functions taking
a single-level pointer to a named struct: the model mirrors the C struct as
``#[repr(C)]`` from its definition, and — because the byte-fuzz differential
can't fabricate a valid instance — these are *gate-only*, verified solely by
the whole-program gate on real instances (``--apply`` + ``--gate-build`` /
``--gate-run``). Subclass casts, pointer-field chasing, and function-pointer
method tables stay out of scope (flagged, not rewritten).

Toolchain: ``cc`` and ``rustc`` on PATH. Network only via the injected
sampler (``--live``).

The machinery lives in :mod:`cgir.ffi` (ir / driver / gate / targets.rust /
sources.c); this module assembles the pair, keeps its historical public
names importable, and holds the pair-specific prompt + orchestration.
"""

from __future__ import annotations

import subprocess  # noqa: F401  (re-exported surface: tests patch rcr.subprocess.run)
import tempfile
from pathlib import Path
from typing import Any

from cgir.ffi.driver import (
    _driver_source,
    differential,
    exported_symbols,
)
from cgir.ffi.gate import (
    _gate_build_run,
    whole_program_gate,
)
from cgir.ffi.ir import (
    _C_INFO,
    TYPE_MAP,
    CEntry,
    FfiEntry,
    _toposort,
)
from cgir.ffi.sources.c import (
    DECL,
    PARAM,
    PTR_PARAM,
    SCALAR_RE,
    STRUCT_PTR,
    _extract_struct,
    _parse_param,
    _patch_source,
    _source_root,
    _struct_defs,
    c_rust_worklist,
    compile_oracle,
    link_back,
    probe_context,
    suspect_global_reads,
)
from cgir.ffi.targets.rust import (
    _assemble_winner_bodies,
    _build_rust_staticlib,
    _rust_type,
    _split_rust_items,
    contract_check,
    extern_block,
    rust_signature,
    try_rustc,
)
from cgir.rewrite import Sampler, run_search_loop

__all__ = [
    "DECL",
    "PARAM",
    "PTR_PARAM",
    "SCALAR_RE",
    "STRUCT_PTR",
    "TYPE_MAP",
    "_C_INFO",
    "CEntry",
    "FfiEntry",
    "_assemble_winner_bodies",
    "_build_rust_staticlib",
    "_driver_source",
    "_extract_struct",
    "_gate_build_run",
    "_parse_param",
    "_patch_source",
    "_rust_type",
    "_source_root",
    "_split_rust_items",
    "_struct_defs",
    "_toposort",
    "build_c_rust_prompt",
    "c_rust_worklist",
    "compile_oracle",
    "contract_check",
    "differential",
    "exported_symbols",
    "extern_block",
    "link_back",
    "probe_context",
    "run_c_rust",
    "rust_signature",
    "suspect_global_reads",
    "try_rustc",
    "whole_program_gate",
]


def build_c_rust_prompt(e: CEntry, context: str = "", callees: list[CEntry] | None = None) -> str:
    ctx = f"\n{context}\n" if context else ""
    ptr_rule = ""
    if any(t.startswith("ptr:") for t, _ in e.params):
        ptr_rule = (
            "\n- Pointer params are raw C pointers into a caller-owned byte buffer "
            "(`*const u8` read-only, `*mut u8` may be written). Use `unsafe` with "
            "explicit bounds — read/write exactly the bytes the C reads/writes, never "
            "past them, and handle a null or zero-length buffer without dereferencing. "
            "A `char*` is a NUL-terminated C string."
        )
    callee_rule = ""
    if callees:
        sigs = "\n".join(f"  {c.name}({', '.join(t for t, _ in c.params)})" for c in callees)
        callee_rule = (
            f"\n- This function calls other functions that are ALREADY available "
            f'via C FFI (declared for you in an `extern "C"` block above your '
            f"function — do not redeclare them): \n{sigs}\n  Call them exactly as the "
            f"C does, inside `unsafe {{ ... }}`. Do NOT reimplement them."
        )
    struct_ctx = struct_rule = output_rule = ""
    if e.struct_defs:
        defs = "\n\n".join(e.struct_defs[k] for k in sorted(e.struct_defs))
        struct_ctx = f"\nThe C struct(s) it takes by pointer:\n```c\n{defs}\n```\n"
        struct_rule = (
            "\n- The pointer params reference REAL C structs (received at runtime). "
            "FIRST write faithful `#[repr(C)]` Rust mirrors of the struct(s) above — "
            "same field order and same field sizes (a C pointer field you do not "
            "dereference can be `*mut u8` or `usize`; a `char x[N]` can be `[u8; N]`; "
            "an `int` is `i32`). Then dereference with `unsafe { (*p).field }`. Getting "
            "the layout wrong will be caught downstream, so mirror it exactly."
        )
        output_rule = (
            "- Output the `#[repr(C)]` struct definition(s) you need, then the one "
            "function item — no markdown fences, no `use` statements, no prose."
        )
    else:
        output_rule = (
            "- Output ONLY that one function item, no markdown fences, no `use` "
            "statements, no extra items, no comments about the translation."
        )
    return f"""Translate this C function into Rust.

```c
{e.source}
```
{struct_ctx}{ctx}
Contract: deterministic, no I/O, no globals, no heap allocation visible to
the caller. It is called through C FFI; the exact item you must produce is:

{rust_signature(e)} {{
    ...
}}

Rules:
{output_rule}
- Preserve C semantics exactly: two's-complement wrapping arithmetic where C
  could overflow (use wrapping_add/wrapping_mul/wrapping_shl etc.), C
  integer-division/shift behavior, and identical branch conditions.
- The function must never panic for ANY input (no unwrap, no plain arithmetic
  that can overflow-panic, no divide-by-zero path C does not have).{ptr_rule}{struct_rule}{callee_rule}
- If the C references macros or globals you cannot see, translate the visible
  logic faithfully anyway."""


def run_c_rust(
    index_dir: Path,
    c_source: Path,
    *,
    sampler: Sampler,
    c_flags: list[str] | None = None,
    k: int = 3,
    n_trials: int = 300,
    pointers: bool = False,
    include_nonleaf: bool = False,
    structs: bool = False,
    budget_usd: float | None = None,
    ledger_path: Path | None = None,
    log: Any = lambda _: None,
) -> dict[str, Any]:
    """Regenerate ``c_source``'s pure functions in Rust, verified end to end.
    With ``include_nonleaf`` the worklist covers functions that call other
    worklist functions, processed callees-first. With ``structs`` it also
    covers single-struct-pointer functions — verified only by the
    whole-program gate on real instances (the isolated differential can't
    build a valid struct), the model mirroring the struct as ``#[repr(C)]``.
    Rides :func:`cgir.rewrite.run_search_loop`."""
    flags = c_flags or []
    workdir = Path(tempfile.mkdtemp(prefix="cgir-crust-"))
    entries, excluded = c_rust_worklist(index_dir, c_source, pointers, include_nonleaf, structs)
    orig = compile_oracle(c_source, [e.name for e in entries], workdir, flags)
    have = exported_symbols(orig, [e.name for e in entries])
    for e in entries:
        if e.name not in have:
            excluded.append((e.component_id, "original symbol not exported (platform/#ifdef)"))
    entries = [e for e in entries if e.name in have]
    by_name = {e.name: e for e in entries}
    probe = probe_context(c_source, entries, workdir, flags)
    counter = {"n": 0}

    def _callees(e: CEntry) -> list[CEntry]:
        return [by_name[c] for c in e.callees if c in by_name]

    def make_prompt(e: CEntry) -> str:
        return build_c_rust_prompt(e, probe.get(e.component_id, ""), _callees(e))

    def evaluate(e: CEntry, cand: str) -> tuple[str, str, dict[str, Any]]:
        counter["n"] += 1
        callees = _callees(e)
        # Prepend extern "C" decls so the candidate's calls resolve; they bind
        # to the original C at verify time (oracle, RTLD_GLOBAL) and the
        # rewritten Rust after link-back.
        source = extern_block(callees) + cand
        dylib, err = try_rustc(
            source, workdir, f"{e.name}_{counter['n']}", allow_undefined=bool(callees)
        )
        if dylib is None:
            return "rustc", err, {}
        # Arity always checked; purity relaxed for non-leaves (calls look
        # impure) and struct-pointer functions (deref through a repr(C) mirror).
        err = contract_check(cand, e, check_purity=not callees and not e.gate_only)
        if err:
            return "contract", err, {}
        # Struct-pointer functions can't be byte-fuzzed into a valid instance;
        # the whole-program gate (--apply --gate) is their authoritative check.
        if e.gate_only:
            return "ok", "", {"regenerated_as": f"rust:{e.name}", "verify": "gate-required"}
        err = differential(orig, dylib, e, n_trials, seed=42)
        if err:
            # "Inconclusive" means the ORIGINAL C faults on most fuzzed inputs,
            # so its precondition isn't expressible to the byte-fuzzer (valid
            # array indices only a real caller supplies — e.g. ts_bm's
            # subpattern). The candidate already matched on every in-contract
            # input the fuzzer did find; route it to gate-only, like a struct
            # pointer, instead of rejecting — the whole-program gate (--apply
            # --gate) is then its authoritative check. A real mismatch is still
            # a rejection (differential returns "mismatch", not "inconclusive").
            if err.startswith("differential inconclusive:"):
                return (
                    "ok",
                    "",
                    {"regenerated_as": f"rust:{e.name}", "verify": "gate-required", "note": err},
                )
            return "differential", err, {}
        return "ok", "", {"regenerated_as": f"rust:{e.name}", "verify": "differential"}

    loop = run_search_loop(
        entries,
        build_prompt=make_prompt,
        evaluate=evaluate,
        sampler=sampler,
        id_of=lambda e: e.component_id,
        k=k,
        budget_usd=budget_usd,
        ledger_path=ledger_path,
        report_meta={"lang": "c-rust", "c_source": str(c_source), "n_trials": n_trials},
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
    return loop
