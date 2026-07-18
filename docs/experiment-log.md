# Rewrite-readiness experiment log

Empirical grounding for the pack/verify direction. Method: for N components
of `camera-tracking`, remove the body and ask Sonnet 4.6 to reimplement it
from context, splice into a shadow repo, run the component's linked tests.
Two conditions: **pack** (CGIR contract bundle, no implementation) vs
**file** (whole source file with the body stubbed — the naive-agent
baseline). Harness: `scratchpad/rewrite_experiment.py`.

## Round 1 (Sprint 18 pack — spec + callee interfaces only)

| condition | pass | avg context |
|---|---|---|
| pack | 4/12 | ~219 tok |
| file | 8/12 | ~3,360 tok |

Failures were **missing data shapes, not missing algorithms**: the model
reconstructed ray casting and OAuth flows correctly but guessed `p.x`
where `Point` is a tuple, missed config-dict keys, missed module constants.
Diagnosis → enrich the pack with type closure, docstrings, raises.

## Round 2 (Sprint 23 pack — + type closure, docstrings, raises, aliases)

| condition | pass | avg context |
|---|---|---|
| pack | **6/12** | ~266 tok |
| file | 8/12 | ~3,360 tok |

The two flips (`point_in_polygon`, `joint_angle`) are exactly the
type-shape failures the enrichment targeted — including
`Point: TypeAlias = tuple[float, float]` in the bundle made the model
unpack correctly. **Pack now matches file on the type-shape class at ~13x
less context.** `get_summary`: 141 tok (pack) vs 20,198 (file), both pass.

The residual 6 failures split cleanly:

1. **Semantics pinned only by tests** (`update_iou_tracker`, `validate`,
   `topo_sort`) — fail under *file too*. No context of surrounding *code*
   reveals exact expiry behavior / tie-break order / error attributes.
   Fix: linked test source in the pack (Sprint 25) or `algorithm` bullets.
2. **Body-level free-name closure** (`default_pipeline` node constants,
   `authorize_url` config keys, `get_daily_rollup` snapshot fields) —
   file passes, pack fails. Current closure pulls names from the *signature/
   return* types only; these need free names referenced in the *body*.
   Fix: extend module-constant closure to body free-references.

## Round 3 (Sprint 25 pack — + linked test source via covered_by)

| condition | pass | avg context |
|---|---|---|
| pack | **8/12** | ~470 tok |
| file | 8/12 | ~3,360 tok |

**Pack now ties the full-file baseline at 1/7th the context** — and passes
three components the file condition *fails* (`update_iou_tracker`,
`zones_for_points`, `topo_sort`). Those are test-pinned semantics: the
linked tests encode the exact expiry behavior / tie-break order that the
surrounding *code* never states. The contract bundle is, for that class,
**better than the raw file, not just smaller.**

Progression as enrichment landed: **4 → 6 → 8 / 12**.

Remaining 4 pack failures:
- `validate` — fails under file too (semantics beyond types+tests in a
  single-function splice).
- `default_pipeline`, `get_daily_rollup`, `authorize_url` — file passes,
  pack fails. All in the **body free-name closure** bucket: module
  constants / config-dict keys referenced in the *body*, not the signature.
  This is the one scoped, un-built enrichment left.

## Round 4 (Sprint 27 pack — + body free-name closure)

| condition | pass | avg context |
|---|---|---|
| pack | **9/12** | ~820 tok |
| file | 8/12 | ~3,360 tok |

Same-module constants and small helpers the *body* references are now
included (e.g. `_cfg()`'s body reveals the config-dict keys that
`authorize_url` reads). `authorize_url` flipped to pass. **Pack now
*exceeds* the full-file baseline at ~4x less context.**

Final progression: **4 → 6 → 8 → 9 / 12**.

Residual 3:
- `validate` — fails under file too; genuinely hard in a single-function
  splice (interdependent validation of a graph structure).
- `default_pipeline` — the test pins an exact template structure the model
  can't reproduce without seeing the template itself (a data fixture, not
  code the contract names).
- `get_daily_rollup` — the linked test asserts on a snapshot's exact
  field shape produced elsewhere; needs cross-component fixture context.

All three are "the answer is a specific data structure defined elsewhere,"
not a contract-comprehension gap.

## Takeaway (Python, behavioral oracle)

Monotonic evidence (4→6→8→9): **an enriched contract bundle beats full-file
context at ~4x less** — matching or exceeding it by including exactly the
semantic pieces CGIR identifies (types, linked tests, module context)
rather than dumping the file. The remaining failures are exact-data-fixture
cases, not comprehension gaps. This is the evidence base for the
pack → verify → gate loop.

Cost: rounds 1–4 ~$0.55 total (Sonnet 4.6).

---

# TypeScript — contract-preservation benchmark

The Angular frontend's specs are Angular-CLI stubs (`expect(x).toBeTruthy()`)
— a blind behavioral oracle. So instead of "tests pass," the oracle here is
**cgir verify's contract check**: splice candidate → rescan → contract-diff;
pass = effects *and* kind unchanged. Deterministic, no test runner.
Harness: `scratchpad/contract_bench.py`. 12 components (thin HTTP-service
wrappers → 31-line orchestration methods), pack vs stubbed-file, Sonnet 4.6.

| condition | contract-preserved | avg context |
|---|---|---|
| pack | **10/12** | ~57 tok |
| file | 9/12 | ~381 tok |

**Pack matches-or-beats the full-file baseline at ~7x less context** — the
same shape as the Python result, replicated on TypeScript with a different
(contract) oracle. The trivial service wrappers preserve trivially under
both; pack *won* on `ReaderComponent.load` (15L).

The 2 failures (`ReaderComponent.translate`, `onFormat`) fail under **both**
conditions — so not a pack deficiency — and they're instructive about *TS
precision*, not the LLM:

- `translate`: original classified `pure_function []` because CGIR's TS
  cross-service DI resolution is weak (`this.chaptersService.translate(...)`
  doesn't resolve to an effectful callee), so the original contract
  under-counted effects. The rewrite added a `console.log` → `io`, flagged
  as drift. The "contract change" is partly CGIR's own under-detection.
- `onFormat`: original `effect_adapter [io]` → rewrite dropped the logging
  → `pure_function []`. A genuine (if minor) contract change, and `io`
  being sensitive to a single `console.log` makes it brittle.

**Honest read:** on TS the contract oracle measures a *mix* of LLM fidelity
and CGIR's TS effect precision. The headline (pack ≈ file at 7x less) holds;
the failures pointed at the next TS improvement — DI-aware cross-service call
resolution. Cost: ~$0.13 (Sonnet 4.6).

**Follow-up (landed):** DI-aware resolution now resolves `this.<field>.<method>`
via constructor-injected field types. On the frontend, the misclassified
orchestration methods (`translate`, `onFormat`, `load`) went from
`pure_function []` to `orchestrator ['calls_effectful']` — their true
contract. The distribution shifted 9-pure/8-adapter → 14-pure/5-orchestrator/
8-adapter. This removes the CGIR-precision confound the benchmark exposed.

## Round 2 (DI-corrected contracts, pack unchanged)

Re-run against the DI-corrected index. The headline numbers *dropped* —
pack **6/12**, file **8/12** — and that is the honest, informative result:

| condition | contract-preserved | avg context |
|---|---|---|
| pack | 6/12 | ~106 tok |
| file | 8/12 | ~381 tok |

The pre-DI 10/12 was **inflated by under-detection**: those orchestration
methods were mis-read as `pure_function []`, so *any* rewrite trivially
preserved a hollow contract. With precise contracts (`orchestrator
[calls_effectful]`), preservation now requires the rewrite to actually wire
the service call through the injected field. Every new pack failure had the
*same* signature: the model called `this.chaptersService.translate(...)`
while the real field is `this.chaptersApi` — a **hallucinated DI field
name**, so the call didn't resolve and the effect silently dropped. The
file condition passed exactly when the visible constructor let the model
copy the right name. Diagnosis → the pack names the *callee*
(`ChaptersService.translate`) but not the *receiver field*.

## Round 3 (+ DI receiver bindings in the pack)

The pack now renders each DI callee as `this.<field>.method(...)` — the
field resolved from the target class's `{field: type}` map (the TS analog of
Python's body free-name closure). One scoped enrichment
(`pack._interface_line` + `cli._call_receivers`):

| condition | contract-preserved | avg context |
|---|---|---|
| pack | **11/12** | ~109 tok |
| file | 8/12 | ~381 tok |

Five components flipped back to pass — the model now reproduces the exact
service wiring. **Pack beats the full-file baseline at ~3.5x less context**,
the same shape as Python, now on genuinely-precise contracts rather than
hollow ones.

The lone residual (`onFormat`) fails under **file too**: the original logs
in its RxJS error callback (`console.error` → `io`), and the rewrite
reproduced the service call but dropped the incidental logging. That's the
known `io`-from-`console` brittleness (a debug log is a first-class effect),
not a pack gap. Cost: rounds 2–3 ~$0.25 (Sonnet 4.6).

**Takeaway:** the DI fix converted an *inflated* benchmark into an *honest*
one, which immediately surfaced the next concrete enrichment (receiver
bindings) — and landing it restored pack's lead. This is the same
monotonic loop the Python rounds followed, and it demonstrates the
contract-preservation oracle catching a real precision regression the moment
it appeared.

## Rung 3 (vision ladder): the small-model benchmark

The economic-thesis experiment: **sample k=3 from Haiku 4.5 → contract
filter (incremental verify) → component tests → escalate failures once to
Sonnet 4.6.** Worklist: every module-level pure function in
`camera-tracking` with direct test coverage (17 components, 3–101 lines —
pose classification, analytics rollups, a metrics DSL, IoU tracking,
point-in-polygon). Harness: `benchmarks/rung3_rewrite.py` (tracked;
replaces the lost scratchpad harness). Results:
`benchmarks/rung3-results-camera-tracking.json`.

| arm | plug-in rate | Haiku-only | escalated | unsolved | cost |
|---|---|---|---|---|---|
| **translate** (source in context — the rung-4 mechanics proxy) | **17/17 (100%)** | 17 | 0 | 0 | ~$0.014/component |
| **spec** (contract+docstring only, body hidden) | **12/17 (71%)** | 10 | 2 | 5 | ~$0.015/component |

Whole run — 34 component-arms, 102+ generations, every candidate
contract-verified and test-run — cost **$0.49** (Haiku $0.41, Sonnet
$0.08). 27 of 29 solved arms passed on Haiku's *first* candidate.

Translate-arm rewrites are genuine restructurings, not copies: mean
normalized similarity to the original **0.49** (range 0.18–0.73), all 17
passing tests.

The five spec-arm failures are the "honest ceiling" made concrete —
**contract + docstring underdetermine behavior**, and the oracles caught
every one deterministically:

- 4× killed by tests: unstated semantics (a zone-stats counting rule, DSL
  validation rules and error-message shapes, a missing-field check).
- 1× killed by the *contract stage*: `topo_sort` — see the audit below;
  this kill does not hold up as a save.

Anti-vacuity control: a deliberately wrong candidate (correct signature,
garbage math) passes the contract stage and fails the test stage — the
two filters measure different things and both bite.

### Post-run audit (2026-07-18): what did the contract stage actually buy?

Across all 57 attempts: **29 passed, 21 killed by tests, 7 killed by the
contract stage — and on audit, none of the 7 contract kills was a
demonstrated save.** All 7 were `raise`-visibility artifacts of lexical
effect detection:

- `topo_sort` (4 kills, left "unsolved"): the writeup above originally
  claimed Haiku added "an effect the original doesn't have." **Wrong** —
  the original raises `CycleError` *through* `graphlib.TopologicalSorter`,
  which lexical detection can't see, so the indexed contract says
  `effects: []`. Candidates with an explicit raise were rejected for
  matching the real behavior more visibly than the original. (The tests
  pin `CycleError` exactly and would have adjudicated fine on their own —
  a replayed explicit-`ValueError` candidate fails `test_topo_cycle_raises`
  deterministically.)
- `registry.get` (3 kills, then escalated): killed for effect-*loss* —
  almost certainly candidates using bare `_REGISTRY[node_type]` indexing,
  which raises the same `KeyError` the test demands (`match=` passes) but
  with no lexical `raise`. Behaviorally-correct candidates plausibly
  blocked; the Sonnet escalation was likely unnecessary. Not provable
  post-hoc: the harness didn't persist candidate bodies (now fixed).

Honest scoring, then: on a well-tested Python repo, the *tests* were the
oracle; the contract stage contributed cheap pre-filtering (7 avoided
test runs) and **zero demonstrated semantic saves, with 3 likely false
rejections**. `raise`-drift should inherit the confidence-tier treatment
I/O effects already have (lexical vs verified) before it's allowed to
kill candidates. What this run does *not* measure at all: components
without tests (the contract is the only gate there — deliberately
excluded from this worklist) and cross-language rewrites (tests don't
transfer; contracts do). Both need their own experiment before the
verify layer's rewrite-loop value is established.

**Takeaway:** verification being free and deterministic converts cheap
generation into search that *works*: 100% plug-in success at about a cent
per component when the model can see the source (the C→Rust shape), 71%
from the contract alone. Model quality affected yield, not correctness —
nothing wrong ever got through; it just cost one escalation or stayed
unsolved.

## Rung 3b: ablation, uncovered components, and the differential seed

Three follow-ups to the audit's open questions (2026-07-18; all artifacts
in `benchmarks/`).

### A. The no-cgir ablation (whole-file prompts, tests-only gate)

Same 17 components, same k=3 + escalation; the contract verdict was
*recorded* on every attempt but never enforced.

| condition | translate | spec | Haiku input tok | escalations | run cost |
|---|---|---|---|---|---|
| cgir (pack + contract→tests) | 17/17 | 12/17 | 116k | 2 | $0.49 |
| ablation (file + tests-only) | 17/17 | 13/17 | 214k | 4 | $0.61 |

- **Yield: parity.** Translate is identical; spec is 12 vs 13 with
  different misses (pack's type closure solved the two
  PoseFeatures-heavy components file-context missed; file-context solved
  two DSL/stats components whose semantics live in sibling code, plus
  topo_sort — see below). No honest yield claim for cgir here.
- **Cost: pack wins.** ~46% fewer Haiku input tokens, half the
  escalations, 20% cheaper overall — same shape as the earlier
  pack-vs-file rounds, now at Haiku prices.
- **The audit's false rejections are now measured, not suspected.** With
  the contract not gating, *test-passing* winners appeared for exactly
  `topo_sort` and `registry.get` — both of which the recorded contract
  verdict would have rejected for raise-visibility drift. 2 confirmed
  false rejections; still 0 demonstrated contract saves on tested code.
- Contract-pass-but-tests-fail: 30 attempts — the other direction
  (contract ≠ semantics) at full strength, as expected.

### B. Uncovered components (contract-only gate) + differential check

The terrain rung 3 excluded: 24 uncovered pure Python functions
(computational modules only, largest-first; API/frontend/ML-backend
excluded — their `pure_function` labels are a separate precision
question). Translate arm, contract as the only gate: **24/24 passed**
(23 Haiku, 1 escalation, $0.36).

Then `benchmarks/differential_check.py` — the rung-5 seed: winning
candidate vs original on 300 random inputs synthesized from type
annotations (deep-copied args, NaN-aware compares, exception-type
matching, nondeterminism self-check):

| verdict | n |
|---|---|
| equivalent | 16 |
| **mismatch** | **1** |
| unsynthesizable (ndarray/Any/object/torch) | 7 |

The mismatch is the whole argument in one function:
`_check_reaching`'s rewrite returns `None` where the original raises
`ZeroDivisionError` on an empty pose window — a silent crash→no-signal
behavior change, invisible to the contract, caught in seconds by
differential replay. **Contract-only false-pass rate on checkable
winners: 1/17 (~6%).**

### Verdict on "does this justify cgir?"

- **Justified:** the worklist/enumeration layer (nothing else finds
  pure+covered candidates), pack economy (≈2x cheaper input, fewer
  escalations, same yield), the verify/splice plumbing, and — new —
  differential replay as the oracle for untested code (one real catch
  on its first outing).
- **Not justified as tuned:** raise-effect drift as a hard kill. On
  tested code it produced 2 measured false rejections and 0 saves.
  Fix before rung 4: raise drift inherits confidence tiers (lexical
  raise ≠ verified raise), or downgrades to report-only in verify.
- **Quantified honestly:** contract-only gating on untested code lets
  ~6% wrong rewrites through — usable as a *pre-filter*, not an oracle.
  The rung-5 differential harness is no longer speculative; it works.

## Rung 4: C -> Rust cross-language regeneration (SQLite)

The first cross-language artifact (2026-07-18; harness
`benchmarks/rung4_c_to_rust.py`, results
`benchmarks/rung4-results-sqlite.json`). Worklist: every pure *leaf*
function in the sqlite3.c amalgamation whose ABI is scalars only — 34
callable after platform/#ifdef exclusions. Pipeline: Haiku 4.5 writes a
`#[no_mangle] extern "C"` implementation (exact FFI signature supplied —
the cbindgen-shaped mechanical part) → **rustc** (filter 1, free) →
**cgir's Rust adapter** contract-scans the candidate (filter 2: pure +
arity — the REGENERATED_AS record) → **differential vs the real compiled
SQLite** (filter 3: 300 random scalar inputs via ctypes; the C oracle is
the amalgamation built with `-DSQLITE_PRIVATE=` so originals are called
directly, no reimplementation drift; trials run in a child process so a
Rust abort can't kill the harness) → one Sonnet escalation carrying the
compiler error or counterexample.

**26/34 (76.5%) verified equivalent — 22 Haiku-only, 4 escalation —
$0.22 for the whole run (~$0.0066 per solved component).** Solved
includes genuinely subtle functions: `sqlite3LogEst`,
`sqlite3IntFloatCompare` (i64/double comparison edge cases),
`countLeadingZeros`, varint length coding. Stage kills across 45 failed
attempts: rustc 10, differential 35, contract 0.

The escalation loop turned out to *extract missing constants from the
oracle*: Haiku guessed `SQLITE_VERSION_NUMBER` (3046000); the
counterexample said "C returned 3053003"; Sonnet shipped the corrected
constant. Same for `sqlite3_keyword_count` (147). The feedback channel
literally carries the invisible context.

The 8 unsolved, categorized — every one caught deterministically, none
wrong-but-accepted:

- **Invisible compile-time context (6)**: `sizeof()` of structs the
  prompt can't see (`sqlite3BtreeCursorSize`: C says 296, Rust guessed
  168), token-code macros (`allowedOp`), generated tables
  (`sqlite3ParserFallback` — the Rust candidate panicked on a negative
  index and the child-process guard caught the abort), a ctype lookup
  table (`sqlite3IsIdChar` — failed on exactly 1/300 inputs: `'$'`).
- **Edge-case semantics (1)**: `sqlite3LogEstAdd` diverged on exactly
  one input pair — `(32767, 32767)`, i16 saturation vs C wraparound.
  The differential found it.
- **Malformed output (1)**: `nodeHash` — Haiku wrote C-style Rust and
  never recovered; rustc filtered every attempt.

Honest caveats: two solved components (`sqlite3_release_memory`,
`sqlite3_threadsafe`) are constant-returning under this build config, so
their solves are trivial; the contract stage killed nothing in this run
(rustc and the differential did the work — its value here is the
REGENERATED_AS bookkeeping and the purity scan, not filtering); and
scalar-ABI leaves are the easy 34 of SQLite's 583 pure functions —
pointers/structs are the next frontier and need real FFI marshaling.

**Takeaway:** the full vision-loop exists end to end for the first time —
enumerate from the graph, regenerate cross-language with a cheap model,
verify mechanically (compiler → contract → differential-vs-original),
escalate with evidence. Failures are not noise: they map one-to-one onto
the vision doc's stated ceilings (the preprocessor, invisible sizeof,
generated tables), which means the next unlock is *context enrichment*
(macro expansion + sizeof provisioning in the pack), not better models.
