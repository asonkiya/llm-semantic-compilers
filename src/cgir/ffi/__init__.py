"""The FFI rewrite core — language-neutral machinery for cross-language
regeneration (docs/design-ffi-pipeline.md).

A rewrite pair = a source-language binding (worklist, behavioral reference,
context, apply) + a target-language binding (signature rendering, compile,
contract scan, assembly) + shared verification (the differential driver, the
whole-program gate, and — for interpreted sources — trace replay). All pairs
ride :func:`cgir.rewrite.run_search_loop`.

Layout:

- :mod:`cgir.ffi.ir` — the signature IR: scalar registry, param tokens, entries.
- :mod:`cgir.ffi.driver` — the dylib-vs-dylib fault-trapping differential.
- :mod:`cgir.ffi.gate` — the whole-program gate (build/run recipes).
- :mod:`cgir.ffi.targets` — target-language bindings (rust).
- :mod:`cgir.ffi.sources` — source-language bindings (c; python planned).

`cgir.rewrite_c_rust` remains the assembled C→Rust pair and re-exports its
historical public names from these modules.
"""
