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

### Rung 4 post-run audit (2026-07-19, user-prompted)

Four suspicions checked; one solve demoted, the rest strengthened.

1. **Harsher differential: 0/26 winners flipped.** All winners re-verified
   at n=2000 with NaN/±Inf/just-past-i64-max doubles added to the edge
   set (the original run tested no NaN — `sqlite3RealToI64` and
   `sqlite3IntFloatCompare` turn out genuinely correct on those edges).
   `--recheck` is now a harness mode: re-verifies stored winners against
   the current differential, no API cost.
2. **One vacuous solve, demoted: `sqlite3HeapNearlyFull`.** It reads
   global state (`AtomicLoad(&mem0.nearlyFull)`) that the harness never
   mutates, so *no* trial count can falsify a constant-returning Rust.
   The upstream cause is cgir's C purity ceiling (global read missed —
   the documented aliasing limit) — here it produced an untestable
   "equivalence". Headline correction: **25 substantive + 1 vacuous of
   34 (73.5% substantive)**. Related but legit: `sqlite3_threadsafe`,
   `sqlite3_keyword_count`, `sqlite3_libversion_number`,
   `sqlite3_release_memory` are constant-under-config — real
   equivalences, just trivial ones (4 of the 25).
3. **Zero-arg theater fixed.** A no-argument function has one observable
   point; the worker now runs 1 trial for those instead of 300 identical
   calls dressed up as coverage.
4. **Silent drops now recorded.** The worklist builder was skipping
   sqlite3.c pure leaves with pointer/array ABIs or unparseable
   declarations without a trace. Full accounting: **400 pure leaf
   functions → 258 pointer/array ABI, 60 not scalar-parseable, 45
   shell.c statics, 2 void → 35 scalar candidates → 34 callable.** The
   scalar worklist is the easy 8.5% of SQLite's pure leaves; the pointer
   mass is where rung 4's next phase lives.

Also noted: the escalation-extracts-constants mechanism (celebrated
above) is double-edged — it is *overfitting to the oracle*, which is
correct exactly when the function is a true constant and dangerous for
state-dependent functions like `HeapNearlyFull`. An orchestrator should
gate constant-hardcoding escalations on the function being genuinely
closed over visible inputs.

## Rung 4++ : compiler-probe context, then SQLite with Rust *inside* (2026-07-19)

Two upgrades aimed straight at the founding vision ("map a C codebase,
rewrite components in Rust, plug them in seamlessly").

### Compiler as context oracle (benchmarks/rung4_c_to_rust.py)

Rung 4's misses were invisible *compile-time facts*, not model failures.
So instead of modeling the C preprocessor, ask the build itself: for each
function's referenced ALL-CAPS/mixed-case macros, `sizeof` targets, and
file-scope tables, generate one probe program that `#include`s the
amalgamation and prints every value, iteratively dropping probes the
compiler rejects (locals, function-like macros — their `#define` text is
supplied instead), one level of macro recursion (`IdChar` ->
`sqlite3CtypeMap`). 26/35 components enriched with real values:
`TK_*` token codes, `sizeof(BtCursor)=296`, the 256-byte
`sqlite3CtypeMap`, the 187-entry `yyFallback` table.

Rerun, same worklist and filters: **plug-in 76.5% -> 85.3% (26 -> 29 of
34)**, and the wins are exactly the previously-"invisible-context"
failures: `sqlite3IsIdChar` (needed the ctype table — was failing on
`'$'`), `sqlite3HeaderSizeBtree` and the `sizeof` cases, `allowedOp`
(the fabricated `TK_*` constants, now probed). Cost unchanged (~$0.20).
Residual 5: genuine edge-case semantics (`LogEstAdd` i16 saturation) and
two functions indexing tables too large for the leaf abstraction. Same
harsh differential (NaN/Inf, n>=300); `HeapNearlyFull` now correctly
*fails* (the probe removed the vacuous pass — it reads global state).

### Link-back: sqlite3 rebuilt with the Rust inside (benchmarks/rung4_linkback.py)

The step that makes "plugged in" literal rather than simulated. Compile
the 29 winners into one Rust staticlib; patch the amalgamation to rename
each C definition to `<name>__cgir_replaced` and emit an extern prototype
(plain-static functions had no separate declaration), so every existing
call site resolves `<name>` to the Rust symbol at link time; build two
real `sqlite3` shells (stock vs Rust-inside) with identical flags; run a
27-line SQL battery chosen to exercise the replaced functions (tokenizer
identifier classing on `$`-idents, hex literals, LIKE, FTS5 MATCH,
int/float comparison edges, `CAST double->int`, varint-heavy 500-row
storage, ORDER BY planning, `integrity_check`).

**Result: byte-identical output, and `PRAGMA integrity_check` returns
`ok` on the Rust-inside build.** `nm` confirms all 29 symbols resolve to
Rust text (`T _allowedOp`) with the C originals sidelined
(`T _allowedOp__cgir_replaced`) — the calls genuinely reach the Rust.
The state-dependent `HeapNearlyFull` is on an explicit DO_NOT_LINK list
(rung-4 audit): its Rust is only equivalent in the untouched state, so it
must never be linked. The 29 rewritten functions are saved at
benchmarks/rung4-artifacts/sqlite_rust_functions.rs.

**What this demonstrates:** the entire vision loop, end to end, on a real
150k-LOC C database engine — map (scan) -> select scalar-ABI pure leaves
-> regenerate in Rust with a cheap model + compiler-probed context ->
verify (rustc -> contract -> differential vs compiled original) -> **link
in place and prove the assembled program is behaviorally identical.**
Cheap models wrote Rust that is now *running inside SQLite*, and a
deterministic battery says you can't tell.

## Rung 4+++ : pointer ABIs + a fault-trapping driver (2026-07-19)

Two upgrades widening the C->Rust surface past scalars.

### Fault-trapping compiled differential driver

The ctypes worker was replaced by a self-contained C driver generated per
function: it `dlopen`s both libraries, installs a `sigaltstack` +
`sigaction(SA_ONSTACK)` fault handler, and guards every call with
`sigsetjmp` so a SIGSEGV/SIGABRT becomes a recorded trap, not a process
death. Two payoffs: (1) no more macOS crash-report spam — functions that
index a table by their raw argument (`sqlite3ParserFallback`) used to
segfault the worker; the driver now traps in-process and the altstack
survives even stack-corrupting faults (re-verified: 0 new crash reports).
(2) contract-aware semantics — if the C *original* faults on an input,
that input is out-of-contract (the C itself is UB there) and is skipped;
only the candidate faulting where the original ran cleanly is a real
divergence, with a minimum-valid-comparisons guard against vacuous passes.

### Pointer parameters (--pointers)

`const`/mut `char*` (C strings) and `u8`/`unsigned char`/`void*` byte
buffers are now fuzzed: the original and candidate get *separate* copies
of an identical random buffer (strings NUL-terminated within 64 bytes,
binary buffers fully random over 4 KB), and equivalence requires matching
return value AND matching post-call buffer contents — so a
write-through-pointer divergence is caught, not just the return. The Rust
candidate gets `*const u8`/`*mut u8` params and an unsafe-bounds
instruction. Struct pointers (`sqlite3*`, `Vdbe*` — the other 211 leaves)
stay excluded; they need real instances.

**Worklist 34 -> 71 (scalar leaves + 37 pointer leaves). Plug-in
61/71 = 86%** (58 cheap, 3 escalation), $0.52. **Pointer functions
specifically: 29/37** — `fts5GetU16/U32/U64`, `readInt16/readInt64`,
`sqlite3Strlen30`, UTF-8 length, and more, each verified by the
dual-buffer differential. The 10 misses are all caught, none silently
passed: real byte-exact mismatches on hash/collation functions
(`strHash`, `binCollFunc`, `fts5HashKey`) that need table context, plus a
few candidate-quality rustc failures (duplicate-name, snake_case-deny).

Scalars + pointers together, ~80 of SQLite's 400 pure leaves (20%) are
now addressable end to end; struct-pointer ABIs are the frontier beyond.

## Rung 4++++ : the at-scale non-leaf sweep — SQLite's pure subgraph in one pass

The founding vision, exercised at scale (2026-07-19). `cgir rewrite --lang
c-rust --non-leaf --pointers --apply` over SQLite's pure-function subgraph:
rewrite each to Rust, verify by differential vs the compiled original, and
link the winners back in — including functions that call other rewritten
functions.

**Sweep: 116/147 solved (79%) for $1.17**, k=3 Haiku + Sonnet escalation.
**25 of the 36 in-scope non-leaf functions** rewritten — notably the whole
FTS Porter-stemmer cluster (`fts5Porter_Vowel`, `fts5Porter_Ostar`,
`doubleConsonant`, `hasVowel`, `m_gt_0` …), a genuine connected subgraph.

**Assembled proof: 92 Rust functions link into a real sqlite3 that passes a
byte-identical SQL battery** (recursive CTEs, LIKE, CAST edges, ORDER BY
planning, FTS5 `MATCH`, FTS3 `MATCH 'stem*'` which drives the Porter stemmer,
`PRAGMA integrity_check`). Of those, **19 are non-leaf — Rust calling Rust
inside SQLite** (e.g. `fts5Porter_Ostar -> fts5PorterIsVowel`,
`sqlite3_stricmp -> sqlite3StrICmp`, `estLog -> sqlite3LogEst`), each verified
against the original C and correct as an assembled whole.
`benchmarks/rung4_nonleaf_battery.py` builds both shells and diffs them.

### The honest finding: per-function verification ≠ whole-program correctness

Linking all 104 non-state-reading winners *crashed* SQLite. Bisection +
category analysis pinned it on ~12 engine-internal functions —
`sqlite3MemSize` and the allocation/btree/pager metadata readers. These pass
the differential: it feeds the C original and the Rust candidate *identical*
random buffers, so both read the same garbage from the (absent) allocation
header and agree — but in a live engine a wrong `sqlite3MemSize` corrupts
malloc tracking and segfaults. Their contract depends on hidden state (the
size word *before* the pointer) that random-input fuzzing cannot model.

This is the rung-5 lesson, now measured at scale: **the differential is
necessary but not sufficient. Whole-program assembly + a behavioral battery
(or, better, capture/replay of real allocations) is the actual gate**, and
memory-introspection functions should be excluded until it exists. Excluding
that class, the remaining 92 assemble cleanly and pass. A robustness fix
landed alongside: `_patch_source` now handles symbols with several
`#ifdef`-guarded definitions (SQLite's allocator variants) instead of
asserting exactly one.

**Takeaway:** cheap models rewrote a connected chunk of a 150k-LOC C engine
to Rust in one pass — 92 functions, 19 of them calling each other in Rust —
and a real SQLite built from them is behaviorally indistinguishable. The
sweep also drew a precise line around where per-function verification stops
being enough.

## Rung 5 at scale: the whole-program gate for context-dependent functions

The at-scale sweep left an open gap: ~12 memory-path functions pass the
isolated differential but crash a live SQLite, and the fix was a crude
name-based exclusion. Capture/replay closes it properly (2026-07-19,
`benchmarks/rung4_program_gate.py`).

You cannot replay a recorded *pointer* — its address and heap are gone by
replay time. So replay the real *workload* instead: link each candidate into
its own SQLite, one function at a time, and run the SQL battery. Survive
byte-identical with no crash -> whole-program-safe; crash or diverge ->
rejected. The isolated differential stays a cheap pre-filter; this is the
authoritative acceptance test.

**Result over the 104 non-state-reading winners: 102 verified, 2 rejected —
`sqlite3MemInit` and `sqlite3MemSize`, both SIGSEGV — caught automatically
with no name heuristic. The assembled 102-function SQLite passes the battery
byte-identical.**

The gate is strictly better than the earlier `Mem|Malloc|Size|Vdbe|Btree|…`
name filter, which conservatively excluded 12 and left 92: the gate rejects
*only* the two functions that actually break and recovers the other ten
(e.g. `sqlite3StrIHash`, `sqlite3Utf16ByteLen`, Vdbe helpers) that the name
filter wrongly dropped. And the two it rejects are exactly the ones whose
contract reads hidden allocation metadata the random-input differential can't
model — pinpointed by running them, not by guessing from their names.

**Takeaway:** the vision's verification stack is now layered and honest —
compiler + cgir contract scan + isolated differential as fast per-function
pre-filters, then the whole-program replay as the gate that admits a function
only if a real program built from it is behaviorally indistinguishable. On
SQLite that yields 102 cheap-model-written Rust functions provably safe to
run inside the engine, with the exact 2 that aren't named and explained.

### Whole-program gate generalized into the product (`--gate-build`/`--gate-run`)

The SQLite-specific gate is now a first-class, project-agnostic capability:
`cgir.rewrite_c_rust.whole_program_gate(c_source, winners, entries,
build_cmd, run_cmd, run_input)` and CLI flags `--gate-build`/`--gate-run`
/`--gate-input`. The user supplies a build recipe (placeholders `{source}`
patched C, `{lib}` Rust staticlib, `{out}` binary) and a run recipe
(`{out}`, optional stdin). On `--apply` each surviving winner is verified by
building+running the real program with only it replaced and requiring
byte-identical output to stock; only the verified set is linked. The
isolated differential stays the cheap per-function pre-filter; this is the
authoritative gate. Proven end to end via the CLI on a 3-function chain
(`quad->sumsq->square`, all gate-verified) and unit-tested (accepts a correct
candidate, rejects a wrong one as `diverged`). `benchmarks/rung4_program_gate.py`
remains the SQLite reproducer.

## Struct-pointer ABIs (`--structs`, 2026-07-20)

The isolated differential can only verify functions whose whole input is
synthesizable from bytes: scalars and char*/byte buffers. A function taking a
pointer to a named struct can't be fuzzed that way — a random buffer isn't a
valid instance (fields have invariants, pointer fields must point somewhere
real). So struct-pointer functions ride the whole-program gate exclusively:
the model writes its own `#[repr(C)]` mirror of the struct from the C
definition, and the only acceptance test is that a real program built with the
function replaced stays byte-identical to stock. There is no per-function
differential for them — `gate_only` is set, the differential is skipped, and
`--apply` without `--gate-build`/`--gate-run` refuses to claim they're verified
(it prints a note and links them unproven only if you insist).

Worklist: `c_rust_worklist(..., structs=True)` adds single-level struct-pointer
params (`struct Ymd *p` or a typedef `DateTime *p`; `**` and scalar pointers
excluded), extracts the C struct definition (plus shallowly-referenced structs,
depth 2, capped) and hands it to the prompt. CLI: `cgir rewrite --lang c-rust
--structs`. On SQLite the flag surfaces ~298 leaf / 381 with `--non-leaf`
struct-pointer pure functions on top of the scalar+pointer set.

**End-to-end proof (live, Haiku 4.5).** A 2-function unit — `rect_area` and
`rect_perimeter`, both `int f(struct Rect *)` — rewritten and linked in one
command:

    cgir rewrite --lang c-rust --structs --live --apply \
      --gate-build 'cc main.c {source} {lib} -o {out}' --gate-run '{out}'

2/2 solved for $0.002, both verified by the whole-program gate on real `Rect`
instances, both linked into `geom.c` (nm-proven the symbols come from Rust),
final program byte-identical to stock C. The model's `#[repr(C)] struct Rect`
mirror is what makes the ABI line up; a **wrong** layout (fields transposed) is
caught by the gate as `diverged` — unit-tested.

**Assembly bug found and fixed.** Combining N struct winners into one staticlib
collided: each winner emits its own `#[repr(C)]` mirror of the shared struct,
so a flat concatenation defined `struct Rect` twice (rustc error). The
per-function gate never hits this (one function per staticlib); only the final
`link_back` does. Fix: `_assemble_winner_bodies` splits each winner into
top-level items and dedups type-defining items (struct/enum/union/type) by name
— first definition wins — while functions stay at top-level scope so
winner-to-winner non-leaf crate calls still resolve. The scalar/pointer path
(no type items) returns the original flat concatenation unchanged.
Regression-tested (assembler dedup + a two-struct-winner staticlib build).

**Honest scope.** The tractable case is a *flat* struct read/written by value
of its fields. The bulk of SQLite's struct-pointer functions are not this:
subclass casts (`sqlite3_vtab_cursor*` -> `carray_cursor*`), pointer-field
chasing across the heap, and function-pointer method-table dispatch. Those stay
out of scope — flagged, not rewritten. `--structs` widens the addressable set
to flat-struct leaves verified on real instances; it does not claim the general
struct-graph case.

## Gate-only routing for differential-inconclusive functions (2026-07-20)

Some pure functions have a precondition the byte-fuzzer can't satisfy: `ts_bm`'s
`subpattern(u8 *pattern, int i, int j, int g)` indexes `pattern[i+g-1]`,
`pattern[j-1]` etc., valid only because its caller (`compute_prefix_tbl`) keeps
`i+g == patlen`. Fuzzed with random ints, ~91% of trials fault out of bounds
(trapped, skipped) and the handful of non-faulting comparisons are on
out-of-bounds *garbage the C read without segfaulting* — so the isolated
differential reports a `mismatch` (e.g. `subpattern(buf,-226,0,-201) orig=1
rust=0`) on a translation that is actually correct.

This is the same verification-boundary class as struct pointers: unverifiable
in isolation, but decidable by the whole-program gate on the real caller. So we
route it there instead of rejecting:

- `differential()` now flags **inconclusive** when the fuzzer overwhelmingly
  produced out-of-contract inputs (`orig_faults + both_faults >= trials/2`) with
  almost no clean comparisons (`compared < trials/4`) — keyed on the *original's*
  fault rate, not the candidate, so a real mismatch on a fuzzable function is
  still a rejection.
- `run_c_rust`'s `evaluate` treats an inconclusive verdict like a struct
  pointer: accept the candidate as `verify="gate-required"` (a provisional
  winner) rather than a `differential` kill. The candidate already matched on
  every in-contract input the fuzzer *did* find.
- The CLI labels these honestly (`solved N/M (K gate-required — not
  differential-verified, need --gate)`), routes them through
  `whole_program_gate` on `--apply` when a gate is configured, and **warns**
  when they'd be linked without one.

End to end through the product: `cgir rewrite --lang c-rust --pointers --live
--apply --gate-build 'cc bm_main.c {source} {lib} -o {out}' --gate-run '{out}'`
on `subpattern` → solved in 1 attempt (differential inconclusive → gate-required),
whole-program gate verifies byte-identical Boyer-Moore good-shift tables over 9
patterns, linked. A flipped-compare variant is rejected by the gate as
`diverged`. The Linux `lib/` batch is now effectively 11/11 through one command.

## The FFI rewrite core + Python→Rust, proven with a real model (2026-07-23)

The C→Rust engine was refactored into a language-neutral core (`cgir/ffi/`,
docs/design-ffi-pipeline.md) so a new language pair costs a binding + prompt +
recipes, not a 1,400-line engine. Four milestones:

- **M1** — extract `cgir/ffi/` (ir / driver / gate / targets.rust / sources.c);
  `rewrite_c_rust.py` reassembled on it. Pure refactor: the 21 c-rust tests
  pass unchanged and the CLI dry-run is byte-identical to pre-refactor HEAD on
  SQLite (603 lines).
- **M2** — `ffi/replay_ffi.py`, the ReplayOracle: replay recorded `(args,
  result)` pairs against a candidate cdylib over the C ABI. Five marshalling
  conventions were verified by live rustc+ctypes experiment, not assumed: i64
  with a mandatory range check (ctypes silently wraps `2**63`); `(ptr,len)`
  strings (never NUL-terminated CStr); Rust-allocated `RustBuf{ptr,len,cap}` +
  `cgir_buf_free` for string returns (output size is unboundable from input);
  bitwise-with-NaN-class float equality; and panic isolation in the harness
  (`panic=abort` + a child that announces each index before the FFI call, so a
  SIGABRT is a per-input rejection with a counterexample and the parent
  respawns on the tail).
- **M3** — the Python→Rust pair. Eligibility (`ast` on real source: params +
  return in `int|float|bool|str|bytes`) → worklist → a prompt carrying the
  exact FFI signature + the RustBuf skeleton + the i64-overflow rule → rustc
  (`panic=abort -C overflow-checks=on`) → symbol-export check → replay against
  captured traces. No isolated fuzz differential (there is no compiled
  "original" — the Python isn't a dylib); the recorded test I/O is the oracle,
  so the verified property is agreement on the recorded, non-raising inputs.
- **M4** — `--apply`: assemble winners into one cdylib (the shared RustBuf
  prelude deduped by item name across string-returning winners), emit a ctypes
  wrapper module, splice each Python body with a thin delegating wrapper, and
  gate on hard contract drift *outside* the rewritten set (drift on the
  rewritten functions — dropped annotations, the new ctypes call — is expected
  and downgraded) plus the repo's own full pytest run (authoritative).

**Real-model dogfood.** Live Haiku on the fixture repo, one command
(`cgir rewrite --lang python-rust --capture --live --apply`): capture real test
I/O → Haiku writes Rust → replay-verify against the captured pairs → compile to
a cdylib → splice ctypes wrappers → the repo's own pytest passes with Rust
inside. **4/4 for $0.005**, no escalation. Notably Haiku's `fnv1a` chose `u32`
masking (matching Python's 32-bit semantics exactly, no overflow) over the
obvious i64, and `shout` reproduced the RustBuf prelude verbatim with a
`len == 0` guard — both replay-verified, then run for real by the test suite.

**Honest scope.** v1 is scalar + `str`/`bytes` leaf functions; the verified
property is agreement on the recorded inputs (weak-evidence functions are
excluded via `--min-traces`). Multi-winner symbol collisions across modules,
packaged-repo wrapper imports, and `list`/tuple/`Optional` params are the
documented next steps. But the thesis holds: the differential driver never
reads a source language, so the same core now drives two pairs — C→Rust
(fuzz-verified) and Python→Rust (replay-verified) — and a third is a binding.

## Linux kernel, more ambitious: crypto primitives (2026-07-24)

Beyond the earlier `lib/` utilities, a compute-heavy target: the kernel's
`crypto/` + `lib/crypto/` (~185 `.c`). Scan: **2,852 components, 2,376 pure
(83%)** in ~6s — ciphers and hashes are overwhelmingly pure transforms.

**Liftability (the honest wall, re-confirmed at a harder target).** 48 functions
have a fuzzable scalar/pointer ABI; a compile-filter against a small kernel-type
shim lifts only **4/45 standalone**. The other 41 are build-entangled exactly as
`lib/` was, but more so: they need allocator calls (`kvfree`/`vfree`), request
contexts (`aead_request_ctx`), globals (`fips_enabled`, `EINPROGRESS`), the
AES S-box *table* (`subw`), or an incomplete `struct serpent_ctx`. Crypto is
83% pure but its *extractable* pure-leaf surface is ~9% of the ABI-eligible set.

**The real rewrite.** The two that matter are genuine AES primitives —
`mul_by_x` / `mul_by_x2` from `lib/crypto/aes.c`, the GF(2⁸) `xtime`/`x²time`
field arithmetic at the heart of MixColumns (`(x<<1) ^ (y>>7)*0x1b` packed over
four bytes in a `u32`). Live: **2/2 solved by Haiku for $0.002**,
differential-verified against the compiled kernel. Haiku's Rust used
`wrapping_shl`/`wrapping_mul` correctly for the C multiply semantics.

**Speed.** These are hot bit-ops (one `mul_by_x` per column per AES round).
Benchmarked as compiled dylibs (C `-O2` vs Rust `-O`), dlopen'd, over a
data-dependent loop:

| primitive | Linux C | Haiku Rust | ratio |
|---|---|---|---|
| `mul_by_x`  | 2.312 ns | 2.326 ns | 0.99× |
| `mul_by_x2` | 2.692 ns | 2.644 ns | 1.02× |

Identical — both compile to the same few instructions. The rewrite is
verified-equivalent *and* performance-neutral (and there's no FFI tax: unlike
the Python→Rust ctypes boundary, this is C-ABI-to-C-ABI, the way the kernel
would actually link it). The honest read: the pipeline correctly and freely
rewrites the kernel's genuine crypto primitives — the binding constraint stays
*extractability* (getting self-contained TUs out of the build), not the model
or correctness or speed.

### The lifter: tree-shake in-file deps into a compilable TU (2026-07-24)

Extractability was the wall, so `cgir/ffi/sources/c_lift.py` attacks the part of
it that's *in the same file*: `lift_symbol(source, fn, shim)` pulls `fn` plus
every file-scope definition it transitively references — tables, `#define`s,
consts, helper functions — into one TU, brace-matched by regex (robust to the
kernel macros — `____cacheline_aligned`, `__alias(...)` — that defeat a real C
parse). The poster child is `subw`: it reads the 256-byte file-scope `aes_sbox`
table, so the isolated extract won't compile; lifted *with* the table it does,
and the pipeline rewrites it end-to-end **live for $0.006** (`probe_context`
injects the S-box values, Haiku embeds them as `const AES_SBOX: [u32; 256]`, the
differential verifies against the compiled kernel).

**Re-running the compile-filter over all 45 ABI-eligible functions, now with the
lifter: 4/45 → 5/45.** Exactly one function (`subw`) was gated purely on an
in-file table, and the lifter unlocks exactly it — no regressions (the other
four baseline lifts still compile). The honest, slightly deflating finding is
*why it's only +1*: kernel crypto's entanglement is overwhelmingly **external**,
not in-file. The other 40 need allocators (`kmalloc`/`kvfree`/`vfree`), request
contexts (`aead_request_ctx`, `crypto_skcipher`), or globals (`fips_enabled`,
`EINPROGRESS`) — none of which live in the function's `.c`, so a file-scoped
tree-shake can't reach them. The lifter's real leverage is where the blocker is
an in-file table or helper (single-file userspace libraries, amalgamations,
embedded lookup tables), which the kernel's allocator-heavy glue mostly isn't.

One bug the probe caught and the tests now pin: the first cut of
`extract_definition` matched a function's **local variables** (`u32 x = w & …;`
inside `mul_by_x`) as if they were file-scope consts and hoisted them out —
where `w` is undefined — *regressing* two baseline lifts. Fixed with a
comment/string-aware brace-depth check: only depth-0 matches are definitions.
5/45 with zero regressions is the post-fix number.

### The lifter on a real single-file userspace library — tiny-AES-c (2026-07-24)

The kernel finding was that the lifter's leverage is *userspace* single-file
libraries, so: tiny-AES-c (`aes.c`, 572 lines — one S-box `sbox[256]`, an inverse
`rsbox[256]`, the `Rcon` table, and the AES round functions). Running the lifter
against real, non-kernel C surfaced three concrete gaps — each fixed, each tested —
before it produced a live rewrite:

1. **stdint types.** The C ABI parser (`ffi/sources/c.py` `SCALAR_RE`) knew kernel
   spellings (`u8`/`u32`) but not C99 `<stdint.h>` (`uint8_t`/`uint32_t`) — which is
   what essentially *every* userspace single-file library uses. So the pipeline
   rejected all of tiny-AES as "not scalar-parseable". Added the fixed-width types to
   `SCALAR_RE` and to `TYPE_MAP` (`uint8_t→(u8,c_ubyte)`, …); the fuzz harness already
   emitted `uint8_t` via `_C_INFO`, so only parse + Rust-mapping were missing.
2. **Definitions inside comments.** `extract_definition`'s function/table/scalar
   branches matched code *inside* `/* */` — tiny-AES ships commented-out reference
   implementations right beside the macros that replace them. Fixed by running all
   matching against a comment/string-masked copy (offsets preserved; returned text
   sliced from the original).
3. **Function-like-macro over-grab.** tiny-AES defines `getSBoxValue`/`Multiply` as
   *both* a `#define name(args) …` and (under `#if`/commented) a function. The
   function-def regex used `[^;{}]*` for the parameter list, which for the macro *use*
   `getSBoxValue(num)` ran on across newlines into the **next** function's `(...) {` —
   swallowing an unrelated function. Fixed with real paren-balancing (`_match_parens`)
   and requiring `{` immediately after the matched `)`; and the `#define` handling now
   prefers a real function over a function-like-macro alias for it (the common
   optional-inline idiom), matching what the tree-sitter adapter sees.

**Live result:** `Multiply` (the GF(2⁸) multiply) lifted *together with* the `xtime`
helper it calls, and `xtime` itself — both rewritten to Rust and differential-verified
against the compiled C, **2/2 for $0.002** (`--non-leaf`, dependency-ordered: `xtime`
first, then `Multiply` calling the Rust `xtime` via `extern "C"`). This is the
userspace *helper-dependency* analog of the kernel's table case.

**Honest limit this library also showed:** tiny-AES does its actual S-box lookups
through **macros** (`#define getSBoxValue(num) (sbox[(num)])`), not functions — so
there is no function ABI to rewrite for the table reads, lifter or not. Macro-based
table access is a real category the function-level pipeline can't target. The lifter
correctly resolves those to the macro (and, asked, pulls the table the macro indexes),
but a `#define` has nothing to export or verify. The rewritable surface here was the
GF arithmetic (`xtime`/`Multiply`), which the lifter delivered.

### The lifter on the SQLite amalgamation — 270k lines (2026-07-24)

The real amalgamation test: `sqlite3.c` — **269,613 lines, 9.5 MB, one translation
unit**. Target `sqlite3VdbeSerialTypeLen(u32 serial_type) -> u32`, a pure scalar leaf
that indexes the file-scope `sqlite3SmallTypeSizes[128]` table defined ~20 lines above
it but 67k lines into the file. Lifted (shim = `u8`/`u32` + empty `SQLITE_PRIVATE`/
`assert`) → 33-line TU with the table tree-shaken in, compiles standalone, and the
pipeline rewrites it **live for $0.003** (differential-verified vs the compiled C).
One function, pulled clean out of a quarter-million-line file.

**The bug this scale exposed — global brace-counting is fragile.** The file-scope
check (is this match a top-level definition or a local variable?) counted net `{`
depth from offset 0. On 270k lines that count *drifts*: a handful of literal braces —
`'{'` char literals, `}` inside string literals — escape the comment/string mask, and
by line 91k the running depth was +3 where it should be 0, so the real definition was
rejected and the lift returned nothing. Any single unbalanced brace anywhere before
the target breaks a global count. Fix: decide file-scope **locally** — a top-level C
definition's line begins at **column 0** (its return type/qualifier is flush-left);
locals inside a body are indented. `_at_col0` replaces the global `_brace_depth`, needs
no whole-file accounting, and is exactly how SQLite, the kernel, and tiny-AES all write
top-level defs. Regression-tested with a source that hides an unbalanced brace in a
char literal before the target. (Also: `lift_symbol` now masks the source once and
reuses it across every dependency lookup — re-masking 9.5 MB per dep was the cost;
2.4 s → 1.5 s.)

This is the lifter's designed use case landing on the canonical target: a single-file
C amalgamation where a function's only barrier to extraction is a table/helper
elsewhere *in the same file*.

### From anecdote to number: the full-SQLite sweep + `--lift` in the CLI (2026-07-24)

Two asks, in order: quantify the lifter's surface on all of sqlite3.c, then make it a
product surface instead of a scratch script.

**The sweep** (`benchmarks/c_lift_sweep.py`): every scalar-ABI pure function in the
amalgamation, compiled two ways — the function's own definition + shim (*baseline*,
the pre-lifter state) vs the lifted TU. Of **92** such functions:

| | compilable standalone |
|---|---|
| baseline (no lifter) | **46** |
| with the lifter | **77** |

**+32 newly unlocked; 84% of the eligible surface now lifts.** The taxonomy of the
15 that still don't is exactly the external-entanglement wall, in miniature:
OS/platform calls (`flock`, `errno`, Win32 APIs — ~8), globals (`sqlite3Config`,
`Mem0Global`, wsd machinery — ~4), and struct-type deps (2). The one "regression"
is Windows-only (`sqlite3_win32_is_nt`) — its baseline "compiled" only because the
non-Windows `#if` branch is trivial; lifting pulls Windows-branch deps a
preprocessor-naive lifter can't know are dead. Documented limit, correctly excluded.

**Three more real bugs the sweep caught** (all fixed + regression-tested):
- `#if defined(SQLITE_TEST)` at column 0, followed by a brace-opening line, *matched
  as a function definition of* `defined` — dragging a ~300-line block of unrelated
  global state into two TUs and breaking them. Fix: `#`-directive lines are never
  definition sites; `defined` is a preprocessor operator, never a dependency. (Run 1
  reported 72/92 — inflated by this over-pull accidentally providing definitions.)
- `xCeil`/`xFloor`/`sqlite3RealSameAsInt` failed only on **libc/libm declarations**
  (`ceil`, `floor`, `memcmp`). The shim now includes `<math.h>`/`<string.h>` —
  declarations only, universally linkable, and the differential still verifies the
  Rust against the real libm-calling C.
- `percentIsInfinity` broke *because* of the header fix: `memcpy` isn't textually in
  the shim, so the BFS pulled SQLite's conditional `#define memcpy(D,S,N)` override —
  which conflicts with `<string.h>`. Fix: libc/libm names are platform-provided and
  never pulled from the file (a keyword set), which also unlocked `sqlite3IsNaN` and
  `sqlite3IsOverflow`.

**The CLI** (`--lift`, plus `--lift-shim`/`--lift-out`): one command, no pre-existing
index —

```
cgir rewrite --lang c-rust --c-source sqlite3.c --lift sqlite3FtsUnicodeFold --live
```

lifts the function + its transitive in-file deps into a TU, indexes it, and runs the
normal engine on it (non-leaf implied — the lift closure contains the helpers).
Under the hood: `lift_symbols` (multi-symbol, one TU / one oracle dylib), typedef
extraction (simple + struct-body — SQLite's own `u8` → `UINT8_TYPE` chain now
self-resolves, so even a minimal shim works), and a shared mask/extraction cache.

**The live battery — 10 one-command rewrites of newly-unlocked functions, 11 solved,
$0.048 total.** Sampled every 3rd newly-unlocked function. All 10 solved; the
`sqlite3FtsUnicodeFold` lift pulled the unicode case-fold tables *and* its
`remove_diacritic` helper into a 214-line TU and the engine rewrote **both**,
dependency-ordered, for $0.038. The verifier worked for its living: **4 differential
kills** — 3 plausible-but-wrong float candidates on `degToRad` before attempt 4
passed, 1 on `remove_diacritic`. Zero false passes.

The arc, in one line: the lifter took SQLite's one-command-rewritable surface from
46 to 77 of 92 eligible functions, and every sampled unlock rewrote and verified
for tenths of a cent.

### Pointer ABIs + a kernel API shim, then a massive kernel-crypto sweep (2026-07-24)

Two levers, then the honest kernel number.

**Pointer ABIs (`--pointers` in the sweep).** The scalar-only sweep left SQLite's
`char*`/byte-buffer functions on the table. Re-run with pointer ABIs: eligible jumps
**92 → 190**, lifted **77 → 148 (+63 newly unlocked)**. The unlocks are real string
algorithms — `sqlite3StrICmp` (reads the `sqlite3UpperToLower` table), the entire
Porter-stemmer suite (`fts5Porter_*`), UTF-8 validators. The pointer ABIs roughly
double the addressable amalgamation surface.

**A kernel API shim (`benchmarks/kernel_shim.h`).** Kernel functions reference
in-header vocabulary the lifter can't reach (`get_unaligned_le32`, byteorder, errno,
`IS_ENABLED`). The shim provides the *pure-computational* slice — every helper written
to the kernel's real byte semantics (unaligned load/store, `__swab32`), so the
differential compares the Rust against a *correct* C reference, not a stub. It
deliberately provides **no** allocators (`kvfree`/`vfree`), request contexts
(`aead_request`), or per-cipher structs — those are stateful glue, and faking them
would be wrong or need full kernel struct layouts. On the 45 ABI-eligible probes the
shim alone doubled the compilable set **4 → 8**.

**The massive sweep — every `.c` in the kernel's `crypto/` + `lib/crypto/` (216
files).** The number is deflating and *correct*:

| | count |
|---|---|
| functions with a fuzzable scalar/pointer ABI | **89** |
| excluded — struct / multi-level-pointer param | **1,266** |
| excluded — not scalar-parseable / vararg / etc. | 773 |
| compilable with shim + lift | **10** |
| of which, lifting (not the shim) unlocked | 3 |

The kernel's rewritable surface is **tiny and gated by struct-context ABIs**:
1,266 functions take a `struct crypto_tfm *` / `aead_request *` / cipher context —
the dominant shape, which neither lifting nor a compute-shim addresses (that needs
struct-mirroring + per-cipher layouts, a different lever). Of the 89 scalar/pointer
functions, only **10** lift to a compilable TU; the other 79 need allocators, request
contexts, crypto teardown, or globals — external entanglement the shim refuses to
fake. **Lifting adds only +3 over the shim** (the AES `aes.c` in-file-table functions
`subw`/`mix_columns`/`inv_mix_columns`) — because kernel functions rarely depend on
*in-file* tables/helpers; their barrier is external, exactly as the first kernel probe
found, now quantified at 216-file scale.

The two levers are cleanly separated by where they pay off: **lifting → userspace
amalgamations** (SQLite: +32 of 92, in-file tables everywhere), **the shim → the
kernel's compute helpers** (lets pure byte/scalar functions compile at all). Neither
moves the kernel's struct-context wall.

**Live proof the shimmed path verifies, not just compiles:** `dh_check_params_length`
(references the shimmed `fips_enabled`/`EINVAL`) lifted + rewritten + differential-
verified against the shim-C, **$0.0008**, one command:

```
cgir rewrite --lang c-rust --c-source dh.c --lift dh_check_params_length \
    --lift-shim benchmarks/kernel_shim.h --live
```

The honest read for the vision: the kernel's crypto tree has ~2,100 pure functions
and a *verified-rewritable* surface of ~10. The ceiling is the struct-context ABI, not
the model, the lifter, or the shim — and closing it means teaching the differential to
construct valid cipher contexts (`--structs` + per-cipher layouts), which is the next
real lever if the kernel is the target.

### `--structs` on the kernel: measure the struct wall directly (2026-07-24)

The previous sweep *excluded* struct-pointer functions; `--structs` admits them (the
worklist supplies each function's in-file-extracted `struct_defs` to the compile, so a
function counts compilable iff its layout is in-file-resolvable). Across `crypto/` +
`lib/crypto/` (216 files):

| | `--pointers` | `+ --structs` |
|---|---|---|
| eligible functions | 89 | **734** |
| of which struct-ptr | 0 | **618** |
| lift-compilable | 10 | **35** |

Admitting struct ABIs is **8×** the eligible surface (struct-context is 84% of it),
and lifting+layouts takes compilable **10 → 35 (+25)**. The unlocks are *real crypto
compute*, not glue: the entire ECC big-integer `vli_*` suite (`vli_cmp`, `vli_num_bits`,
`vli_test_bit`, `vli_from_le64`, …) and the **Twofish key schedule** (`__twofish_setkey`).
But 583 of the 618 struct functions still don't compile — their barrier is the kernel's
deep type/API web (`container_of`, `IS_ERR`, `__percpu`, `CRYPTO_ALG_TYPE_*`, request
chains), which lives in headers and is *not* faked by the shim. Lifting adds +6 over
the layouts+shim; the struct wall is real, and ~5% of it is in-file-resolvable.

**A bug `--structs` surfaced (fixed + regression-tested).** `vli_cmp(const u64 *left,
…)` parses as `struct:u64` — `u64 *` isn't a byte-pointer element, so it's treated as a
struct pointer. But there's no `struct u64` to extract, so `struct_defs` was empty, and
`gate_only` was defined as `bool(struct_defs)` → **False** → the isolated differential
ran and **KeyError'd** on the `struct:u64` param it has no input-codegen for. Fix:
`gate_only` is now *any* struct-typed param, not "we extracted a layout" — a struct
param can't be byte-fuzzed whether or not we have its definition. `vli_cmp` now lifts,
rewrites, and returns cleanly **gate-required** ($0.0014) instead of crashing:

```
cgir rewrite --lang c-rust --c-source ecc.c --lift vli_cmp \
    --lift-shim benchmarks/kernel_shim.h --structs --live
→ solved 1/1 (1 gate-required — not differential-verified, need --gate)
```

That "gate-required" is the honest verification state for struct functions: the
isolated byte-fuzzer can't build a valid `vli`/`ctx` instance, so behavioral proof
needs the whole-program gate (`--apply --gate-build/--gate-run`) on real instances —
exactly the "next lever" the prior entry named, now with the sweep number attached
(35 compilable, gate-verifiable; behavioral verification still gated).

### `--structs` on SQLite: amalgamation vs kernel — and a hang the scale exposed (2026-07-24)

Ran the same `--structs` sweep on the SQLite amalgamation to test a hypothesis: an
amalgamation keeps *all* its structs in the one `sqlite3.c`, so struct-context
functions should be far more in-file-resolvable than the kernel's ~5% (whose contexts
live in external headers). **The hypothesis was wrong, in an instructive way.**

Full `sqlite3.c` (269k lines), `--pointers --structs --non-leaf`, 602 eligible fns:

| surface | fns | baseline cc | lifted cc | lift made a TU |
|---|---|---|---|---|
| scalar / pointer | 220 | 97 | **162** (+65) | 220 / 220 |
| struct-pointer   | 382 | 1  | **1**        | 380 / 382 |

Two very different stories under one sweep:

- **Scalar/pointer is where lifting delivers: 97→162 (+65, 74% compilable).** Every one
  produced a TU; the 58 misses are external OS calls / globals, not lifting failures.
  Real unlocks: the whole FTS5 **Porter stemmer** suite (`fts5Porter_MGt1`, `_Vowel`,
  `_Ostar`…), `sqlite3StrICmp`, UTF-8 helpers, `binCollFunc`, `estLog` — each pulled its
  in-file table/helper closure into a standalone TU.
- **Struct-context does NOT benefit from the amalgamation.** 380 of 382 struct functions
  *lifted fine* — their layouts are in-file, closures resolve (median TU 66 lines, one
  5,135) — **but 379 of those 380 fail to `cc`.** The amalgamation solves *type
  resolution* and not *isolation*: a struct function's full call+type graph pulls half
  the database engine (Parse/Vdbe/Select/Expr are one giant connected component), which
  won't compile standalone. The one win, `allConstraintsUsed(sqlite3_index_constraint_
  usage *aUsage, int nCons)`, takes a **flat POD array struct** — the un-entangled shape
  that isolates.

So SQLite's struct compile rate (1/382 ≈ **0.3%**) is *lower* than the kernel's (35/734 ≈
4.8%) — because the kernel's "wins" were mostly `u64 *`-misparsed-as-`struct:u64`
pseudo-structs (ECC `vli_*`) with tiny closures, while SQLite's are genuine multi-field
AST structs. **Corrected takeaway: struct-context is the wall in *both* — the kernel
because contexts are external, the amalgamation because in-file contexts are all mutually
entangled. Lifting's robust leverage is the scalar/pointer surface (+65 here), not
struct ABIs, regardless of amalgamation-vs-kernel.**

**The hang this sweep exposed (root-caused + fixed, commit 5d83dbe).** The first
`--structs` run wedged for 1.5h pegging a core with *no compiler children* — a pure-
Python spin. `extract_definition` ran ~7 full-file regex passes over the 9.5 MB source
**per queried name** (~0.65s each, hit or miss). A struct function pulls a closure of
~90 identifiers — most of them struct field names and locals that resolve to `None` but
still pay the full scan → ~48s for one function; a function reaching a god-struct blew
the closure into the thousands and never returned. Scalar/pointer sweeps never hit it
(tiny closures). Fix: `build_def_index()` scans the file **once** (2.8s → 8,430 defs)
into a `name→def` dict; every lookup is O(1). `sqlite3WalkExprList`: >15s (killed) →
**0.01s**. Proven byte-identical to the old per-name extractor (0 mismatches / 529
sampled names). A `MAX_CLOSURE=400` backstop was added but barely bites (2 of 382) — the
struct wall is compile-failure, not closure size. The lesson for the sweep harness: a
per-file `try/except` catches *exceptions* but not *hangs*; the durable fix was making
the inner loop incapable of blowing up, not wrapping it.

**Kernel re-verification with the fixed lifter (same day).** Re-ran both kernel
`--structs` sweeps (crypto/ 148 files, lib/crypto/ 68 files, fresh scans, kernel shim)
on the `build_def_index` lifter to confirm the committed numbers survive the rewrite:
**identical to the row — 731/731 (file,name) rows match, 0 verdict diffs, worklists
693+41, baseline 27+2, lifted 30+5, 0 regressed — and `MAX_CLOSURE` never fires in the
kernel** (its closures are small; the cap exists for amalgamation god-structs). Both
sweeps + scans now finish in ~6 min wall.

One footgun surfaced en route: a first re-run against *stale per-directory indexes*
produced a silently smaller worklist (528+30) — the worklist's defining-file check is a
*basename* proxy (`Path(span.path).name == c_source.name`, `c.py`), and the kernel has
duplicate basenames across directories (`crypto/sha256.c` vs `lib/crypto/sha256.c`), so
index scope shifts which components attribute to a swept file and non-leaf candidacy
cascades from there. Sweep numbers are only comparable under same-scope scans; the
sweep's default (scan the swept tree fresh) is the canonical condition.

### Live rewrite battery over all 163 lifted SQLite TUs (2026-07-25)

Ran the full C→Rust pipeline live on **every** compilable TU the `--structs` sweep
produced (one scan of the TU dir → per-TU: compile-as-oracle → cheap model →
rustc → contract scan → differential fuzz; struct/pseudo-struct params downgrade to
gate-required instead of fuzzing). Scoreboard:

| | |
|---|---|
| TUs run | **163/163** (0 errors) |
| fn components (incl. closure deps) | 171 |
| **solved** | **146 (85%)** — 137 differential-verified, 9 gate-required |
| targets solved | 125/163 |
| total cost | **$1.01** (~$0.007/solved fn) |
| attempt-level kills | 94 differential, 51 rustc — **0 false passes** |

Texture: **21 TUs were multi-function closure rewrites** (the FTS5 Porter suite tops
out at 4 dep-ordered functions per TU). The 9 gate-required solves are the struct fn
(`allConstraintsUsed`) plus the `sqlite3AddInt64/SubInt64/MulInt64/Multiply128/160`
overflow-check family — `i64*` out-params parse as pseudo-structs, so they compile+
rewrite but await the whole-program gate. 18 TUs scanned to zero worklist components
(all `sqlite3_*` public-API wrappers whose target doesn't re-classify as an eligible
pure fn from the standalone TU — a lifter/scan interaction to look at, not a rewrite
failure). The 20 unsolved targets: 16 die at `differential` — dominated by **hash and
bit-twiddling functions** (`fts3StrHash`, `fts5HashKey*`, `sqlite3Get4byte/Put4byte`,
`randomFill`) where the cheap model fumbles C unsigned-overflow/aliasing semantics and
the differential correctly refuses every attempt — and 4 at `rustc` (unicode-table
giants; `sqlite3FtsUnicodeIsalnum` burned the per-TU budget at $0.116).

Two harness facts this battery bought: (1) the credit-wall run proved **clean resume**
(59 prior rows kept, 104 rerun, results merged); (2) it exposed the differential-driver
wedge fixed in the entry below/commit `cc37b24` — `randomFill`'s candidate stalling
SIGKILL delivery under a page-fault storm, now bounded by the driver's own `alarm()`.

### Whole-program gate: all 9 gate-required functions verified (2026-07-25)

The battery left 9 solves marked *gate-required* — struct/pseudo-struct-pointer ABIs
the byte-fuzzer can't exercise (`allConstraintsUsed`, `hasColumn` (`i16*`),
`rtrimCollFunc`, `sqlite3AddInt64/SubInt64/MulInt64`, `sqlite3Multiply128/160`). Ran
the real gate on every one: per TU, a *workload* `main()` appended inside the TU (the
targets are `static`) constructing **real instances** — valid
`sqlite3_index_constraint_usage` arrays, 225-point i64 edge grids over the
`INT64_MIN/MAX` overflow boundaries, 128/160-bit multiply hi/lo checks, collation keys
with trailing spaces and embedded NULs — then
`cgir rewrite … --apply --gate-build 'cc -O1 -w {source} {lib} -o {out}' --gate-run '{out}'`:
stock build first, then each winner linked in alone, stdout required byte-identical.

**Result: 9/9 verified, 0 rejections, 8 linked artifacts (patched C + Rust staticlib +
shared lib), ~$0.035 of fresh rewrites.** The `SubInt64` TU gated both its functions
(Rust `sqlite3SubInt64` reaching `sqlite3AddInt64` via `extern "C"` in one build, and
each verified with only itself replaced).

**Negative control (the pass is not vacuous):** a plausible-but-wrong `sqlite3AddInt64`
(clamps to `i64::MAX` on overflow instead of C's leave-and-flag semantics) fed to the
same gate on the same workload → rejected `diverged`. The workloads discriminate.

This closes the verification ladder for the battery: 137 differential-verified + 9
gate-verified = **146/146 solves behaviorally proven**, still 0 false passes.

### Header-aware lifting: the kernel measurement (2026-07-25)

Built the next lever the kernel probes pointed at: resolve definitions across the
``#include`` closure instead of one ``.c`` file (`resolve_includes` post-order walk +
`build_multi_index` union with per-header caching + cross-file emission ranks; plain-tag
``struct``/``union``/``enum`` + enumerator indexing — the kernel's no-typedef house
style, which the index was blind to). `--lift-include` on the CLI, `--include` on the
sweep. Red-green throughout.

**The true kernel numbers** (crypto/ + lib/crypto/, full tree, arm64 include roots):

| | single-file lift | header-aware |
|---|---|---|
| crypto/ lift-compilable | 30 | **44** |
| lib/crypto/ lift-compilable | 2 | **13** |
| total | 35 | **57 (+63%)** |

Real unlocks: the ECC `vli_*` suite now lifts *with* its real header types (no
pseudo-struct handout), `aria_set_decrypt_key`, DH/RSA/ECDH param helpers, MD5
export/import. lib/crypto's 6.5× is the signal — that's the kernel's most
rewrite-shaped code, and headers were most of its wall.

**Two hard-won lessons en route.** (1) The first two "header-aware" sweeps measured
nothing: the kernel checkout was *sparse* — ``include/`` didn't exist on disk, the
resolver correctly resolved nothing, and the +1/+2 deltas were shim-handling noise.
Materializing the full tree (and raising the include-closure cap 400→2000; sha3.c's
closure needs 534 files before ``crypto/hash.h`` lands) produced the real jump. Check
the substrate before trusting a null result. (2) The remaining ~630 failures are NOT
closure-size (uncapped probes lift in 0.0s, ~7.7k-line TUs) — they die on **shim/header
collision**: `kernel_shim.h` typedefs ``u64`` as ``unsigned long long`` while the pulled
``linux/types.h`` says ``unsigned long`` on arm64, plus compiler-builtin/CONFIG_
vocabulary. Next lever: drop shim lines the closure already defines (shim-vs-closure
dedup), not more pulling.

### Rung 4 begun: the kernel itself as the gated program (2026-07-25)

`benchmarks/kernel_gate/`: an arm64 container (kbuild toolchain + Rust + QEMU — macOS
can't run kbuild natively; Apple-silicon Docker runs arm64 at native speed) and
`stock_leg.sh` — kernel tree copied into a container-native volume (bind-mount builds
crawl), ``defconfig`` + built-in crypto self-tests, QEMU boot, timestamp-normalized
console capture. The captured ``testmgr`` output is the golden stdout for the Rust leg:
a rewritten function compiled ``no_std --emit=obj``, linked via kbuild ``obj-y``, its C
definition sidelined by the existing `_patch_source`, then boot-and-byte-compare — the
same gate shape that went 9/9 on SQLite, with the Linux kernel as the program.

### Rung 4 achieved: a Rust rewrite verified inside the booting kernel (2026-07-25)

`benchmarks/kernel_gate/gate.sh` closes the loop. testmgr turned out to be the wrong
workload — modern testmgr is silent on success, and a no-initramfs boot panics on
rootfs before much runs — so the gate uses a **`late_initcall` probe** instead: it runs
during ``kernel_init_freeable`` (before the panic), calls the target on a fixed 64-point
vector grid, and ``pr_info``s a single deterministic digest. Both legs build+boot under
QEMU; the gate compares that digest.

The target (``cgir_target``, an FNV-1a mix over the 8 bytes of a u64) is defined in C in
the stock leg and by a Rust object in the rewrite leg. The Rust leg: ``rustc --emit=obj
--target aarch64-unknown-none-softfloat -C panic=abort`` → a freestanding object
exporting ``cgir_target`` with **zero undefined intrinsics** (a pure function needs no
kernel runtime, so no CONFIG_RUST machinery); injected via kbuild's ``_shipped``
prebuilt-object rule; the C definition dropped so the call resolves to Rust at link.

| candidate | stock digest | rewrite digest | verdict |
|---|---|---|---|
| `correct.rs` (byte-equal FNV) | `0d6ce7859b3c8aa6` | `0d6ce7859b3c8aa6` | **PASS** |
| `wrong.rs` (FNV prime −1 bit)  | `0d6ce7859b3c8aa6` | `095ed7cd993a80b3` | **REJECT** |

**The negative control earned its keep — the first cut PASSED the wrong candidate.** Two
harness bugs it exposed: (1) the freestanding target's std wasn't in the image, so rustc
E0463'd every run — each ``docker run --rm`` is fresh and the earlier standalone
``rustup target add`` didn't persist (→ baked into the Dockerfile); (2) ``gate.sh``
swallowed the rustc failure (``rustc | tail`` under ``bash -euc``, no ``pipefail``) and
then booted the stock leg's leftover ``Image``, faking a match. Fix: ``pipefail``,
hard-fail on a missing object, and delete ``Image`` before each leg so a failed build
can't boot stale. A gate that can't reject is worse than none; the control is the only
thing that proves it can — the same discipline as the SQLite negative control, and it
caught a real vacuity here.

**Honest scope.** ``cgir_target`` is a *planted* pure function (real algorithm, real
build+link+boot, but not yet a load-bearing kernel function). This proves the
**mechanism** end to end: a cheap-model-shaped Rust rewrite runs in the real kernel and
the kernel's own boot is the judge. The next step points it at a lifted real crypto leaf
(the header-aware set — e.g. a ``vli_*`` or AES helper) with the probe calling it on the
subsystem's own test vectors.

### Real-repo pass: verify-diff + the rewrite engine on open-source repos (2026-07-25)

Ran verify-diff and the live rewrite loop against real OSS repos, both to prove
they work outside fixtures and to find what fixtures hid. They found real bugs —
three, all fixed:

**verify-diff (behavior-preservation gate).** inflection (Python, flat pkg),
python-slugify (Python, `src`-style pkg), bytes.js (JS, mocha), a TS module, and a
jest project. Regression→diverged and refactor→preserving landed correctly on all
of them; jest honestly reported `unverified` (it sandboxes its module registry).
Two bugs surfaced: (1) the Python capture harness ran from a tempdir so pytest
couldn't import the repo's package by name and captured nothing — fixed by putting
`os.getcwd()` on sys.path; (2) the JS harness reconstructed a function *alone*, so
`bytes.parse`'s module-level regex/table were undefined and both versions threw
`ReferenceError` identically → a **false `preserving`** on a genuine regression.
Fixed by evaluating the whole module (old file vs new file) with a real
`createRequire`, exactly as the Python path execs in the module namespace.

**The rewrite engine (live, cheap model).**

| repo | pair | solved | cost | note |
|---|---|---|---|---|
| inflection | python→python | **11/12** | $0.03 | `singularize` unsolved (big regex rule-table) |
| python-slugify | python→python | 2/7 | $0.09 | real refactor (merged two guard clauses), replay-verified |
| inflection | **python→rust** | **5/10** | $0.41 | real string fns → compiled, replay-verified Rust |

Every "solved" is replay-verified against the repo's own recorded test I/O — a
spot-checked `smart_truncate` rewrite genuinely restructured the code (not an
echo), and `dasherize` became a real `#[no_mangle] extern "C"` Rust function with
RustBuf string marshalling. python→rust's unsolved 5 (pluralize/singularize rule
tables, transliterate's unicode maps) resist an exact Rust port — an honest
boundary (stage kills: 17 rustc, 9 replay).

**Third bug, from the rewrite pass:** the slugify run *crashed* — replaying a CLI
function triggered argparse, which raises `SystemExit` (a BaseException, not
Exception), and `replay()`/`differential_replay` caught only Exception, so it
escaped and killed the whole run. Fixed to catch `(Exception, SystemExit)`: one
pathological function is now a failed replay, never a harness death. Same lesson
as the differential-driver alarm and the sweep timeout — isolate per-unit failure.

### Rung 4 on REAL kernel functions: 3 model-rewritten AES routines verified in-kernel (2026-07-25)

Generalized the gate (``gate_fn.sh`` + ``probe/fn_probe.c.tmpl``) to any real
``u32->u32`` kernel function, and pointed the **live cheap-model pipeline** at three
genuine Linux AES primitives from ``crypto/aes_generic.c`` — the model wrote the Rust,
kbuild compiled it with the kernel toolchain, and a ``late_initcall`` probe verified the
boot digest stock-vs-Rust:

| real kernel function | what it is | verdict | digest |
|---|---|---|---|
| `mul_by_x`  | GF(2⁸) ×x, pure bit-twiddle | **PASS** | `574c81fc56c361fb` |
| `mul_by_x2` | GF(2⁸) ×x², pure bit-twiddle | **PASS** | `f4d92da8c6a53765` |
| `subw`      | S-box word transform, **256-entry table** | **PASS** | `2798f60600c34773` |
| `mul_by_x` (poly 0x1c) | deliberately wrong reduction | **REJECT** | `c8612be0…` ≠ stock |

3/3 correct rewrites verified inside a booting kernel, 1/1 wrong rewrite rejected, 0
false results. ``subw`` is the important one: the model **embedded the full 256-entry AES
S-box** in the Rust (the table the C reads from a file-scope array), kbuild built it, and
the boot proved it byte-identical — the lifter's core "tree-shake the table into the
rewrite" value, now demonstrated end-to-end in the real kernel rather than in an isolated
TU. This is the honest upgrade from the planted ``cgir_target``: load-bearing kernel
code, model-generated, kernel-toolchain-compiled, boot-verified.

**And a second subsystem + the gate-required class (``vli_cmp``, ECC).** Extended the gate
to a ``u64``-array ABI (``vli_gate.sh`` + ``vli_probe.c``, exercising the function over a
grid of fixed 4-digit big-integers) and verified ``vli_cmp(const u64*, const u64*,
unsigned)`` from ``crypto/ecc.c`` — the elliptic-curve big-integer comparison:

| real kernel function | subsystem / class | verdict | digest |
|---|---|---|---|
| `vli_cmp` | ECC big-int, **gate-required** (`u64*` pseudo-struct) | **PASS** | `ff35a57b289bf3d5` |
| `vli_cmp` (sign flipped) | negative control | **REJECT** | `af87ef21…` ≠ stock |

This matters because ``vli_cmp`` is exactly the **gate-required** class — a pointer ABI the
isolated byte-differential *cannot* fuzz (the SQLite whole-program gate existed precisely
for this class). Verifying it in-kernel on real ``u64`` arrays closes that loop for the
kernel: **4 real crypto functions across two subsystems (AES + ECC) and two ABI classes
(scalar/table + pseudo-struct), all model-generated, all boot-verified, with negative
controls rejecting in both classes.** The kernel path is now demonstrably: rewrite in
place → kbuild compiles with the real toolchain → the booting kernel is the judge.

**Non-leaf works in-kernel too (``vli`` family).** Verified three more ECC functions in
one build — ``vli_test_bit``, ``vli_num_digits``, and the **non-leaf ``vli_num_bits``**
(which calls ``vli_num_digits``): both rewritten to Rust, assembled into one object, and
the Rust→Rust ``extern "C"`` call resolves at link inside the kernel. PASS
(``7af37e893148c50a``). So the non-leaf case is not a barrier when the callee is itself a
rewrite candidate — the AES ``mix_columns`` non-leaf was blocked only because its callee
``ror32`` is a kernel bitops helper outside the candidate set, a scope limit, not a
mechanism gap.

**Final overnight tally: 7 real Linux kernel crypto functions** (AES: ``mul_by_x``,
``mul_by_x2``, ``subw``; ECC: ``vli_cmp``, ``vli_test_bit``, ``vli_num_digits``,
``vli_num_bits``), all model-generated Rust, all verified inside a booting kernel across
two subsystems, two ABI classes, leaf and non-leaf — with negative controls rejecting in
both classes and 0 false results. See ``docs/overnight-2026-07-25.md`` for the full run.

**A finding that reshapes the kernel strategy.** Probing why ~630 header-lifted kernel
TUs failed the *macOS* compile check showed the failures are a **cross-compilation
artifact**, not a lifter weakness: compiling kernel C with macOS ``cc`` collides Darwin
libc types with kernel types (``__kernel_ssize_t`` vs ``__darwin_ssize_t``) and leaves
annotation macros (``__rcu``) undefined. The lesson: **for the kernel, lifting to a
standalone TU is the wrong tool** — it fights the header ecosystem. The in-place kbuild
gate is right, because kbuild already owns the correct toolchain, flags, and header tree.
The lifter's real domain remains userspace amalgamations (SQLite); the kernel path is
rewrite-in-place + boot-gate, exactly what these three AES results do.
