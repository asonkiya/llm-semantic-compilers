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
run with small models, verify-filtered, is the thesis's proof-of-life
experiment — runnable today (`scratchpad/rewrite_experiment.py`).

**Why the last two weeks weren't a detour:** the gate/pins/verify work is
the trust machinery this vision requires. The contract layer is the
rewrite engine's verifier, shipped first because it's independently
useful — and it funds the credibility of the flagship.

## The ladder (each rung independently valuable)

1. **C adapter** *(in progress — agent-from-docs pattern, like Rust)*.
   The kernel is C; so are the best first targets. Validate on real
   userland C, not the kernel.
2. **`cgir decompose`** — the unshipped flagship: PDG-slice functions into
   functional-core / imperative-shell, i.e. *manufacture* the pure,
   pin-able, rewritable units. All machinery exists (PDG, effects,
   purity, blast radius). Metric: % of a repo decomposable into pure
   components with test coverage.
3. **Small-model benchmark** — rerun the rewrite harness with Haiku-class
   models on pure leaf functions, verify-filtered, same-language first.
   The headline number: *N% plug-in success at $X per component*.
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
