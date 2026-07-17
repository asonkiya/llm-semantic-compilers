# North star: rewriting massive codebases with simple models

The founding vision (Code-IR.md): transform a repository into small,
traceable, language-agnostic ComponentSpec units that an LLM can rewrite,
reassemble, and audit — up to and including mapping something like the
Linux kernel and rewriting components in Rust that plug in seamlessly.

This document is the durable statement of that goal, why everything built
so far serves it, the ladder to it, and the honest ceiling.

## The economic thesis

"Rewrite big codebases with simple models" dies, naively, on one
asymmetry: **cheap generation is worthless without cheap verification.**
A small model produces a plausible Rust rewrite of a C function for
fractions of a cent; if a human or a frontier model must check it, nothing
was saved.

CGIR's contract layer is the missing half: a **deterministic, zero-cost
verifier**. `pack` gives the model exactly the contract it must satisfy;
`verify` + contract-diff + pins reject drift mechanically; `impact --run`
executes exactly the tests that prove behavior. Verification being free
converts rewriting from *generation* into **search**:

    sample k candidates from a cheap model
      → filter through verify / pins / component tests   (free, deterministic)
      → escalate only survivors' failures to a bigger model

Model quality then affects *yield*, not *correctness*. The rewrite
benchmark (docs/experiment-log.md) already showed pack-only context makes
rewrites *beat* whole-file context at 4–7x less input; the same harness
run with small models, verify-filtered, was the thesis's proof-of-life
experiment — now run: rung 3 below (`benchmarks/rung3_rewrite.py`).

**Why the last two weeks weren't a detour:** the gate/pins/verify work is
the trust machinery this vision requires. The contract layer is the
rewrite engine's verifier, shipped first because it's independently
useful — and it funds the credibility of the flagship.

## The ladder (each rung independently valuable)

1. **C adapter** *(✅ landed and SQLite-validated)*. Agent-from-docs round
   two (72/72, promoted, repo-wide external-linkage resolution). First
   scan of the SQLite amalgamation (269,613 lines, one file): **2,663
   components, 7,840 resolved calls, 583 pure functions (21%) — the
   rewrite-candidate pool exists and is enumerable.** The scan also
   surfaced the first genuine scale bottleneck (O(functions x tree)
   function lookup: 7m46s) — fixed with per-file function indexing
   (37s, 12.6x, byte-identical output). `cgir search "kind:pure
   callers:>5"` over SQLite is rung 2's worklist.
2. **`cgir decompose`** *(✅ landed)* — PDG-sliced functional-core /
   imperative-shell suggestions (advisory; the safety net is extract →
   pin → verify). Per-statement effects via the adapters'
   ``classify_calls``; shell = effectful statements + everything data- or
   control-downstream; whole control regions containing effects collapse
   (you can't extract half a loop). **Measured: camera-tracking 120/159
   impure functions decomposable (75%); SQLite 1,015/1,803 (56%).**
   Combined with the 583 already-pure functions, ~60% of SQLite is
   addressable — the rewrite-candidate pool for rungs 3–4.
3. **Small-model benchmark** *(✅ landed — docs/experiment-log.md "Rung
   3")*. Haiku 4.5, k=3, contract-filter → tests → one Sonnet escalation,
   over every test-covered pure function in camera-tracking (17). **With
   source in context (the rung-4 shape): 17/17 plug-in at ~$0.014 per
   component, all Haiku, genuinely restructured (mean similarity 0.49).**
   From the contract alone: 12/17 (71%) — the gap is docstring-
   underdetermined semantics, every failure caught deterministically
   (one by effect-gain drift before tests even ran). Harness:
   `benchmarks/rung3_rewrite.py`.
4. **Cross-language regeneration (C → Rust)** — light up the spec's
   dormant `REGENERATED_AS` / `TRACE_OF` edges: language-agnostic pack →
   Rust candidate → FFI boundary generation (cbindgen-shaped) → link and
   test. Start with pure leaf functions where the ABI surface is scalars
   and structs.
5. **Differential harness** — contract equivalence ≠ behavioral
   equivalence. Capture/replay at the component boundary: record real
   inputs/outputs of the old implementation, replay against the new one.
   The one genuinely new subsystem.
6. **Scale backend** — persistent/incremental graph (the P2 Neo4j-or-
   sqlite thread) once targets exceed in-memory comfort (~1M LOC).

## Target ladder (evidence before ambition)

| target | why | oracle |
|---|---|---|
| **SQLite** (~150k LOC C) | the most-tested codebase in software history | its own test suite = free behavioral oracle |
| curl / redis | real, respected, tractable C | good suites, real I/O boundaries |
| a kernel *subsystem* (e.g. a driver) | the horizon, entered sideways | KUnit where it exists |

The kernel amalgam itself is the horizon, not a milestone. Note
Rust-for-Linux already walks that path by hand — CGIR's contribution is
the *tooling that makes such migrations searchable*, not the migration.

## The honest ceiling

- **Contract ≠ semantics.** CGIR checks effects, types, shape, call
  surface — not behavior. Behavior comes from tests and (rung 5)
  differential replay. A component without either is not safely
  rewritable, full stop; `cgir search "covered:false"` is the map of
  where the vision cannot yet reach.
- **The C preprocessor.** Kernel C is macro-dense and `#ifdef`-mazed;
  tree-sitter parses the surface, not the expansion. Userland C is far
  milder; the kernel-grade answer (preprocessed-translation-unit
  ingestion) is a research rung of its own.
- **Invariants no static contract captures**: lock ordering, memory
  barriers, RCU semantics, performance envelopes. A rewrite can pass
  every CGIR check and still be wrong for the kernel. These need
  domain oracles (differential + stress), not graph analysis.
- **Pointer aliasing** limits purity claims in C; confidence tiers must
  stay honest about it.

## Positioning discipline

One repo, two stories. The gate/pins wedge is the shipped, credible
product; the rewrite flagship earns README space one demonstrated rung at
a time. Never pitch the kernel before rung 4 has a public artifact.
