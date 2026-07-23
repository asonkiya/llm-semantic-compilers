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

import tempfile
from pathlib import Path
from typing import Any

from cgir.ffi.driver import exported_symbols
from cgir.ffi.replay_ffi import Trace, replay_against_dylib, validate_traces
from cgir.ffi.sources.python import PyEntry, python_rust_worklist
from cgir.ffi.targets.rust import RUSTBUF_PRELUDE, rust_signature_ir, try_rustc
from cgir.rewrite import Sampler, run_search_loop

# Verification-build flags: overflow-checks turns a silent i64 wrap into an
# abort (a rejection); panic=abort makes every panic an abort the harness
# isolates. Both were fixed by the M2 marshalling research.
_RUSTC_FLAGS = ["-C", "panic=abort", "-C", "overflow-checks=on"]


def build_python_rust_prompt(e: PyEntry) -> str:
    sig = e.sig
    has_slice = any(p.kind == "slice" for p in sig.params)
    ret_buf = sig.ret.startswith("buf:")
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
- Deterministic: no I/O, no globals, no time/random.{slice_rule}{prelude_rule}"""


def run_python_rust(
    index_dir: Path,
    repo: Path,
    *,
    sampler: Sampler,
    traces: dict[str, list[Trace]],
    query: str = "kind:pure covered:true",
    k: int = 3,
    min_traces: int = 3,
    budget_usd: float | None = None,
    ledger_path: Path | None = None,
    log: Any = lambda _: None,
) -> dict[str, Any]:
    """Regenerate ``repo``'s eligible pure functions in Rust, replay-verified
    against ``traces`` (keyed by component id). Rides
    :func:`cgir.rewrite.run_search_loop`; report shape matches ``run_c_rust``."""
    workdir = Path(tempfile.mkdtemp(prefix="cgir-pyrust-"))
    entries, excluded = python_rust_worklist(index_dir, repo, query)

    # A function is verifiable only with enough valid recorded inputs; drop the
    # rest into `excluded` with a specific reason (the verified property is
    # agreement on the recorded inputs, so weak evidence is no evidence).
    verifiable: list[PyEntry] = []
    for e in entries:
        t = traces.get(e.component_id, [])
        if not t:
            excluded.append((e.component_id, "no captured traces"))
            continue
        reason = validate_traces(e.sig, t)
        if reason:
            excluded.append((e.component_id, reason))
            continue
        if len(t) < min_traces:
            excluded.append((e.component_id, f"only {len(t)} traces (< min-traces {min_traces})"))
            continue
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
        verdict = replay_against_dylib(dylib, e.symbol, e.sig, traces[e.component_id])
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
    return loop
