# Plan: 0.2 → 0.4 (pins, init, impact --run; data-shape contracts; Go)

**Status: all three workstreams landed 2026-07-10** (single sitting; see git
log). Shipping as one 0.2.0 release rather than three.

Three workstreams, landed in this order — each one's machinery feeds the next.
Depth before width: pins + shapes strengthen the gate for the two languages
people can use today; Go then inherits all of it.

## Workstream A — "Make it personal" (→ 0.2.0)

### A1. Declarable contracts (pins)

Comment pragmas pinning *intent*, enforced everywhere the pipeline runs:

```python
# cgir: pure
def score(events): ...        # trailing form also supported
```

Module-level pins at top of file apply to every component in it.

Pins split into two classes with different checkers:

| pin | class | meaning | checked by |
|---|---|---|---|
| `pure` | state (single scan) | kind pure_function, no impure effects incl. transitive | lint, watch |
| `no-<tag>` | state | tag not in transitive effects (closure over spec.calls) | lint, watch |
| `stable-signature` | change (scan pair) | signature/outputs may not change | diff, hook, verify, CI |
| `frozen` | change | no contract field may change; removal is a violation | diff, hook, verify, CI |

Change pins are always evaluated — the pin is the opt-in, no --fail-on needed.
State pins catch violations in newly added code too (drift rules can't).

Design: new `report/pins.py` with `state_violations(specs)` and
`change_violations(old, new)` — pure over specs; transitive effects computed
by closure over `spec.calls` (no schema change beyond the `pins` field).
Wire into lint, the diff CLI, hook, verify; pack renders `Pinned: ...` in the
target header so rewriting agents see the invariant.

Plumbing: adapter pragma extraction (generic row-based helper over tree-sitter
`comment` nodes; `#` / `//` prefixes) → FunctionDecl.pins → node attrs →
`pins: list[str]` on ComponentSpec (schema in both places + Code-IR.md).

Honest limit: a pin is only as strong as effect detection (lexical escapes per
status.md). Confidence tiers are the future systematic fix.

### A2. `cgir init`

One-command onboarding: scan → print "what your repo is" (histogram,
entrypoints, untested-effectful) → write starter `cgir.toml` (never clobbers
without --force) → `--hook` installs the seatbelt → append `.cgir/` to
.gitignore → print MCP + Action next-steps.

### A3. `cgir impact --run`

Execute the impact test list: qualname + trace path → pytest nodeid
(`path::Class::test_x`), subprocess, propagate exit code. Filter to
pytest-collectable names (covered_by can include fixture helpers). v1 runs
pytest only; TS prints the command (runner detection is v1.1).

## Workstream C — Data-shape contracts (→ 0.3.0)

"The rewrite dropped a field from the returned object" becomes drift — the
residual failure class in both benchmarks.

The DI work already built the foundation: `ClassDecl.fields` (name → type)
exists in both adapters, and Python's annotated-class-body extraction already
covers TypedDict/dataclass/pydantic. Additions:

- TS: `interface_declaration` / object type-alias members → ClassDecl.fields.
- Shape lives on the *type* (Class node), not a new spec field. `compute_diff`
  gains a `types` section (added/removed/changed fields per type qualname),
  with `referenced_by` computed via `referenced_type_names` over specs.
- New violation rule `shape-change`: fires when a drifted type is referenced
  in any component's outputs/inputs.
- ScanResult carries `types`; diff CLI reads them from repo_graph.json; hook
  and verify inherit.

v1 exclusions (stated): inheritance (base-class fields), un-annotated
dict-literal returns (literal-key fingerprints v1.1), Optional/default
subtleties beyond the annotation text.

## Workstream B — Go adapter (→ 0.4.0)

Lands after C so GoAdapter implements fields/shape extraction as part of its
contract rather than retrofitting.

- package = directory; `type X struct/interface` → Class (struct members →
  fields — Go composition maps directly onto the DI machinery); method
  receivers attach methods to the receiver's Class; `raise` ≙ `panic(` only
  (Go errors are values).
- Effects: net (net/http), fs (os.*), db (database/sql receivers ×
  Query/Exec), io (fmt.Print*/log.*), nondeterm (time.Now/rand/uuid).
- Same-package cross-file calls (no import in Go): symbol tables for go
  modules sharing a directory are merged (keyed on the module language attr).
- v1 known limits (documented): cross-package import resolution needs go.mod
  module-prefix stripping (follow-up); struct-in-one-file/methods-in-another
  weakens field DI; `select`/`defer` CFG ordering approximated; `go f()` is a
  call site (future concurrency-effect hook).
- Validation ritual: fixture tests mirroring the TS suite, scan a real OSS Go
  repo, byte-identical self-diff on Python/TS indexes.

## Cross-cutting

Every spec-vocabulary change updates Code-IR.md §Data model + both schema
locations + a schema test. Red-green throughout. Each workstream ships as a
minor release (0.2.0, 0.3.0, 0.4.0) — release mechanics are push-button
(RELEASING.md).
