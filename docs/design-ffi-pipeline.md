# Design: the FFI rewrite core — language-neutral rewrite pipelines

Status: **approved; M1–M3 landed** (M1 2026-07-20 — `cgir/ffi/` extracted,
c-rust reassembled on it, 21-test pin + CLI dry-run byte-identical on SQLite.
M2 2026-07-21 — `ffi/replay_ffi.py`: Param/Signature IR, trace validation,
subprocess-batch replay harness with crash-respawn, 14 tests against a
fixture cdylib. M3 2026-07-23 — the Python→Rust pair: `ffi/sources/python.py`
(ast-based eligibility + worklist), `ffi/targets/rust.py` (`rust_signature_ir`,
`RUSTBUF_PRELUDE`, `try_rustc(extra_flags=)`), `rewrite_python_rust.py`
(`build_python_rust_prompt` + `run_python_rust`), `cgir rewrite --lang
python-rust` (dry-run + `--live` + `--capture`/`--traces`); proven e2e on a
committed fixture repo with a fake sampler (4/4 replay-verified incl. RustBuf
string return, bytes slice; wrong + panicking candidates rejected). M4 core
2026-07-23 — `--apply`: assemble winners into one cdylib (prelude deduped),
emit a ctypes wrapper module, splice delegating Python wrappers, expected-drift
gate + full pytest; proven on the fixture with a fake sampler (4/4 applied,
the repo's own pytest green with Rust inside). Real-model dogfood target open). Research grounded in a full seam-map of
`rewrite_c_rust.py`, the `run_search_loop`/`replay.py`/`verify.py` contracts,
and live Python↔Rust FFI experiments (rustc + ctypes smoke tests; results in
§6.2's traps — every load-bearing convention below was verified on-machine,
not assumed).

## 1. Motivation

`cgir rewrite` today has two engines: Python→Python (`rewrite.py:rewrite_repo`)
and C→Rust (`rewrite_c_rust.py`, ~1,400 lines). Adding a language pair
currently means writing another 1,400-line engine. But the seam analysis shows
most of `rewrite_c_rust.py` is not C→Rust logic — it is a **generic
differential-verification engine expressed in C-flavored terms**:

- The fault-trapping driver, fuzzer, dual-buffer compare, gate-only routing,
  and whole-program gate operate on *dylibs and an ABI vocabulary*, not on C.
  The driver dlopens two shared libraries and compares them; it never reads C.
- The param-token grammar (`"int"`, `"ptr:str:const"`, `"struct:Ymd:mut"`) plus
  `TYPE_MAP`/`_C_INFO` is already an implicit, language-neutral signature IR.
- `run_search_loop` is fully opaque (worklist + `build_prompt` + `evaluate`
  injected) and both existing engines already ride it.

**Goal:** factor the engine around an explicit FFI-IR so a new language pair
costs a *signature mapper + prompt template + toolchain recipes* (~100–200
lines + config), not a new engine. Prove the abstraction with a second real
instance: **Python→Rust**, verified by replaying captured real I/O against the
compiled Rust — composing `replay.py` (exists) with the FFI-IR marshalling.

**Non-goals:** containers/objects across the FFI (v1 is scalars + str/bytes),
performance claims (we verify *equivalence*; ctypes overhead can make small
functions slower — the deliverable is a verified translation, benchmarking is
the user's), Windows, build-system ingestion (separate research rung; its
worklists will feed this same engine later).

## 2. Architecture

```
per language PAIR (thin)                 shared core (fat, mostly exists)
┌──────────────────────────────┐         ┌─────────────────────────────────┐
│ SourceLanguage binding        │         │ run_search_loop (rewrite.py)    │
│  worklist: index → [FfiEntry] │         │  k-sample → escalate → ledger   │
│  reference: oracle dylib      │────────▶│ evaluate cascade:               │
│             | captured traces │         │  compile → contract → oracle    │
│  context: probe | introspect  │         │  (+ gate-only / inconclusive    │
│  apply: link_back | wrapper   │         │     routing, unchanged)         │
├──────────────────────────────┤         ├─────────────────────────────────┤
│ TargetLanguage binding        │         │ VerifyOracles:                  │
│  render signature/externs     │         │  DifferentialOracle (dylib×2,   │
│  compile / assemble+dedup     │         │    fuzz, fault-trap) [exists]   │
│  contract scan                │         │  ReplayOracle (traces × dylib,  │
│  prompt semantic rules (data) │         │    subprocess batch)   [new]    │
├──────────────────────────────┤         ├─────────────────────────────────┤
│ toolchain recipes (strings)   │         │ whole_program_gate (recipes)    │
└──────────────────────────────┘         └─────────────────────────────────┘
```

### 2.1 Module layout

```
src/cgir/ffi/
  __init__.py
  ir.py         # ScalarType registry, Param/Signature/FfiEntry (token-compatible)
  driver.py     # differential driver codegen + DifferentialOracle   [moved]
  replay_ffi.py # ReplayOracle: trace marshalling + subprocess batch [new]
  gate.py       # whole_program_gate + _gate_build_run               [moved]
  targets/rust.py   # RustTarget                                     [extracted]
  sources/c.py      # CSource                                        [extracted]
  sources/python.py # PySource                                       [new]
```

`rewrite_c_rust.py` **remains** as the assembled C→Rust pair, re-exporting its
current public names (`CEntry`, `c_rust_worklist`, `differential`, `link_back`,
`whole_program_gate`, `run_c_rust`, …) from the new modules. The existing 21
tests in `test_rewrite_c_rust.py` are the migration pin and must pass
**unchanged** — they define the extraction's red-green contract.

## 3. The FFI-IR (`ffi/ir.py`)

The IR makes today's implicit vocabulary explicit while staying
token-compatible (the driver codegen keeps consuming tokens internally).

```python
@dataclass(frozen=True)
class ScalarType:          # lifts TYPE_MAP + _C_INFO into one record
    name: str              # canonical: "i8".."i64", "u8".."u64", "f32", "f64", "bool"
    bits: int
    signed: bool
    is_float: bool
    # per-language spellings live in the bindings, keyed by canonical name

@dataclass(frozen=True)
class Param:
    name: str
    kind: str              # "scalar" | "cstr" | "buf" | "slice" | "structptr"
    scalar: ScalarType | None = None     # kind == "scalar"
    mutable: bool = False                # pointer kinds
    struct_name: str | None = None       # kind == "structptr"
    # conventions (which pointer kinds a pair uses) are declared by the pair:
    #   C→Rust:      cstr (NUL-terminated, fuzz driver), buf (4096B fuzz)
    #   Python→Rust: slice (ptr+len, UTF-8/bytes)  — never cstr (embedded NULs)

@dataclass(frozen=True)
class Signature:
    params: list[Param]
    ret: str               # "void" | scalar name | "buf"  ("buf" = RustBuf return)

@dataclass
class FfiEntry:            # generalizes CEntry
    component_id: str
    symbol: str
    sig: Signature
    source: str            # original-language source (prompt context)
    callees: list[str] = field(default_factory=list)
    struct_defs: dict[str, str] = field(default_factory=dict)
    context: str = ""      # probe/introspection text for the prompt
    verify: str = "auto"   # "auto" | "gate-only"   (auto = oracle decides;
                           #  inconclusive-differential still routes to gate)
```

Token compatibility: `Param.token` / `FfiEntry.params_tokens` properties render
the existing strings (`"int"`, `"ptr:str:const"`, `"struct:Ymd:mut"`; new
`"slice:str"`, `"slice:bytes"`, ret-`"buf"`), so `_driver_source` and the tests
migrate without a rewrite. Full dataclass-native driver codegen is a later
cleanup, not part of this change.

## 4. The seam protocols

```python
class SourceLanguage(Protocol):
    def worklist(self, index_dir: Path, source: Path, opts: WorklistOpts
                 ) -> tuple[list[FfiEntry], list[tuple[str, str]]]: ...
    def make_reference(self, entries, workdir) -> Reference: ...
    #   C: compile_oracle → dylib.   Python: capture() → validated traces.
    def enrich_context(self, entries, workdir) -> None: ...
    #   C: probe_context (cc probe).  Python: none needed in v1.
    def apply(self, source, winners, entries, out_dir, opts) -> dict: ...
    #   C: link_back.  Python: wrapper-module emission + splice (see §7.5).

class TargetLanguage(Protocol):
    def render_signature(self, e: FfiEntry) -> str: ...
    def extern_decls(self, callees: list[FfiEntry]) -> str: ...
    def compile(self, candidate, workdir, tag, *, allow_undefined=False
                ) -> tuple[Path | None, str]: ...
    def contract_check(self, candidate, e, *, check_purity=True) -> str: ...
    def assemble(self, winners: dict[str, str]) -> str: ...   # type-item dedup
    def prompt_rules(self, e: FfiEntry) -> str: ...           # semantics text

class VerifyOracle(Protocol):
    def verify(self, reference: Reference, artifact: Path, e: FfiEntry,
               trials: int, seed: int) -> str: ...
    # "" = pass; "mismatch …" = reject; "inconclusive …" = route to gate
    # (exact strings preserved from differential() so evaluate() is unchanged)
```

The engine (`run_ffi_rewrite`) is today's `run_c_rust` with the bindings
injected — the evaluate cascade, gate-only routing, inconclusive routing,
stage-kill accounting, and ledger/budget behavior are **unchanged**.

## 5. Instance 1: C→Rust (mechanical migration)

Function disposition (from the seam map):

| Moves to | Functions |
|---|---|
| `ffi/ir.py` | `CEntry`→`FfiEntry` alias, token grammar, `TYPE_MAP`/`_C_INFO`→`ScalarType` registry, `_toposort` |
| `ffi/driver.py` | `_driver_source`, `differential`, `exported_symbols` |
| `ffi/gate.py` | `whole_program_gate`, `_gate_build_run` |
| `ffi/targets/rust.py` | `rust_signature`, `_rust_type`, `extern_block`, `try_rustc`, `contract_check`, `_split_rust_items`, `_assemble_winner_bodies`, `_build_rust_staticlib` |
| `ffi/sources/c.py` | `DECL/PARAM/PTR_PARAM/STRUCT_PTR` + `_parse_param`, `c_rust_worklist`, `_extract_struct`/`_struct_defs`, `compile_oracle`, `probe_context`, `_patch_source`, `link_back`, `suspect_global_reads`, `_source_root` |

Acceptance: all 21 existing tests pass with **zero edits**; `cgir rewrite
--lang c-rust` CLI behavior byte-identical (dry-run output included).

## 6. Instance 2: Python→Rust

### 6.1 Worklist (`ffi/sources/python.py`)

From the index alone: query `kind:pure covered:true`, then parse each spec's
`signature` field (stored as raw annotated text, e.g.
`"clamp(x: float, lo: float, hi: float) -> float"`). Eligible v1: every param
and the return annotated with exactly `int | float | bool | str | bytes`; no
defaults, no `*args/**kwargs`, no `Optional`/unions/containers. Everything
else lands in `excluded` with a reason (same reporting shape as C).

### 6.2 Marshalling conventions (experimentally verified)

| Python | C ABI | Rust | Notes |
|---|---|---|---|
| `int` | `int64_t` | `i64` | **ctypes silently wraps out-of-range ints** (verified: `c_int64(2**63).value == -2**63`) — a wrapped replay could falsely pass. Every recorded int (args *and* results) is range-checked to `[-2^63, 2^63)`; any violation marks the **function** out of scope. |
| `float` | `double` | `f64` | Bit-exact round trip verified incl. NaN payloads, sNaN, −0.0, subnormals. |
| `bool` | `_Bool` | `bool` | ABI-compatible; normalize Python `bool`-in-`int`-slot at capture (`type(v) is bool` → strict type-exact rejection in v1). |
| `str` arg | `(const uint8_t*, size_t)` | `(*const u8, usize)` → `from_utf8` | **(ptr, len), never NUL-terminated CStr** — embedded NULs are real and CStr truncates silently (verified with `"héllo\x00wörld🎉"`). Encode-failure (lone surrogates) at capture ⇒ out of scope. |
| `bytes` arg | same `(ptr, len)` | `&[u8]` | No UTF-8 validation step. |
| `str`/`bytes` return | by-value `#[repr(C)] RustBuf {ptr, len, cap}` + one exported `cgir_buf_free` | `ManuallyDrop<Vec<u8>>` pattern | Output size is unboundable from input (`"ß".upper() == "SS"`), which kills caller-provided buffers. `cap` is required (`Vec::from_raw_parts` with wrong cap is UB). **The RustBuf + free-fn skeleton is baked into the prompt verbatim** — this is exactly the code an LLM gets subtly wrong. Verified through 100k alloc/free cycles. |

Rust compile flags for verification builds: `-C panic=abort
-C overflow-checks=on` — turns silent i64 wraparound in generated Rust into a
detectable crash instead of a wrong answer only some trace would catch.

### 6.3 Reference: captured traces

`replay.capture()` (exists: `setprofile` tracer, deep-copied `(args, result)`
pairs, subprocess-isolated pytest driver) with a validation pass per trace:

- drop traces where any int is out of i64 range / any str fails UTF-8 encode
  (if any trace fails, the *function* is excluded — the Rust version can't be
  proven over the values the Python one handles);
- runtime types must match annotations exactly (v1: `type(v) is float`, no
  int-for-float coercion);
- raising calls are never recorded (tracer only records returns) — so the
  verified property is, explicitly: **agreement on the recorded, non-raising
  inputs**. This is the same epistemic status as the C whole-program gate
  ("byte-identical on the exercised workload"), and is reported as such.
- functions with fewer than `--min-traces` (default 3) valid traces are
  excluded with reason `insufficient-traces` — few traces = weak evidence, and
  we say so rather than claim verification.

### 6.4 ReplayOracle (`ffi/replay_ffi.py`)

Subprocess-per-candidate, **batch of all replays inside one child** (panic
kills a process; subprocess-per-call wastes ~50ms × hundreds):

1. Parent spawns child with the cdylib path + marshalled traces (pickle file).
2. Child emits one JSON line per event, flushed: `{"calling": i}` **before**
   each FFI call, `{"i": i, "ok": bool, "got": …}` after.
3. On abort (panic → SIGABRT under `panic=abort`): parent keeps completed
   verdicts, marks the in-flight index `CRASH` (that input becomes the
   counterexample in the escalation prompt), and **respawns the child on the
   remaining tail** — one panicking input rejects the candidate without
   discarding the rest of the evidence. Per-batch timeout (30s default):
   infinite loops are likewise rejections. (Batch protocol verified end-to-end:
   5-call batch with a panic at index 2 → `ok, ok, CRASH, ok, ok`.)
4. Comparison predicates (verified choices):
   - int/bool: exact (after range check — check first, or wrapping can
     coincidentally match);
   - float: **bitwise equality with all NaNs collapsed to one class**
     (`struct.pack('<d', a) == struct.pack('<d', b)` or both-NaN). Plain `==`
     spuriously rejects every NaN trace; bitwise-only is spuriously strict on
     NaN payloads (not a semantic promise of either language); `==` also hides
     `0.0 vs -0.0`, which *is* observable. This is the unique predicate strict
     where semantics are observable and lenient where they aren't;
   - str: compare UTF-8 **bytes** (also catches invalid-UTF-8 output);
   - bytes: exact bytes.
5. Verdict strings match `differential()`'s exactly (`""` / `"… mismatch …"`),
   so the engine's evaluate cascade and stage-kill accounting are unchanged.
   (No `inconclusive` case: traces are real inputs by construction.)

### 6.5 `--apply`: wrapper emission and the final gate

Applying a winner means splicing a ctypes wrapper in place of the Python body:

```python
def clamp(x: float, lo: float, hi: float) -> float:
    return _cgir_rs.clamp(x, lo, hi)          # thin wrapper, module-level lib
```

plus an emitted `_cgir_rs.py` (loads the cdylib once, defines argtypes/restype
from the FFI-IR, wraps RustBuf returns with `string_at` + `cgir_buf_free`) and
the cdylib artifact.

**Known wrinkle (from the verify.py seam map):** the final gate's contract
diff will see the wrapper as drift — purity may drop and the call surface
changes (`calls` now targets the ctypes shim). This is *expected, by design*
— the same way link-back changes the C binary. Resolution: the apply path
passes an expected-drift profile for spliced components (purity/calls changes
on exactly the rewritten set are downgraded to informational notes), and the
authoritative gate is the **full pytest run** with wrappers in place — the
direct analog of the C whole-program gate. Hard drift on *other* components
still fails.

### 6.6 CLI

```
cgir rewrite --lang python-rust --query "kind:pure covered:true" \
    [--capture | --traces traces.pkl] [--min-traces 3] \
    [--k 3] [--budget-usd …] [--ledger …] [--out …] --live \
    [--apply [--wrapper-out DIR]]
```

Dry-run (no `--live`) prints the eligible worklist + per-function trace counts
(if traces given) + exclusion reasons — same UX as c-rust dry-run.

## 7. Testing strategy (red-green, per CLAUDE.md)

- **M1 (extraction):** the 21 existing c-rust tests are the pin; no new tests,
  zero edits to them. Plus one import-compat test (public names still resolve).
- **M2 (ReplayOracle):** toolchain-gated unit tests against a hand-written
  fixture cdylib: correct fn passes; wrong fn rejected with the counterexample
  input in the feedback; panicking fn = rejection not harness death (respawn
  covers the tail); NaN/−0.0/embedded-NUL traces compare per §6.4; i64-range
  violation excludes the function; empty/insufficient traces report honestly.
- **M3 (worklist):** signature-parse tests: eligible forms, and each rejection
  class (unannotated, containers, Optional, defaults, *args).
- **M4 (e2e dogfood):** camera-tracking (exists at
  `~/Documents/Programming/camera-tracking`; cgir must be installed into its
  venv per the rung-3 procedure — note its venv is Python 3.14, verify compat
  or rebuild it). Target: capture real traces from its test suite, rewrite the
  eligible pure functions to Rust, replay-verify, `--apply`, full pytest green.
  Results → `docs/experiment-log.md` as the rung-3-shaped proof for the pair.

## 8. Milestones

| # | Deliverable | Risk | Acceptance |
|---|---|---|---|
| M1 | `cgir/ffi/` package; c-rust re-assembled on it | Low (mechanical) | 21 tests + CLI byte-identical |
| M2 | ReplayOracle + marshalling + batch harness | Medium (FFI edge cases — but conventions pre-verified) | M2 unit suite green |
| M3 | Python source binding + `--lang python-rust` dry-run | Low | M3 tests + dry-run on a fixture repo |
| M4 | `--apply` wrappers + final gate + dogfood | Medium (expected-drift profile) | camera-tracking pytest green with Rust inside |

Each milestone lands independently; M1 alone is a pure-refactor commit.

## 9. Risks and open questions

- **Contract-scan arity:** str params expand to two ABI slots (ptr+len), so the
  Rust contract check must count IR-expanded arity, not Python arity.
- **Trace coverage is the ceiling:** the verified property is agreement on
  recorded inputs. Low-trace functions are weak evidence — the min-traces
  floor and per-function trace counts keep the reporting honest.
- **camera-tracking venv is Python 3.14** — cgir targets 3.11+; verify or
  rebuild before M4.
- **Windows / i686:** out of scope (matches current c-rust posture). The x87
  sNaN-quieting caveat is noted and irrelevant on our targets.
- **No performance claims:** ctypes overhead can exceed the win for tiny
  functions. The product claim is *verified translation*; speed is the user's
  benchmark to run.
- **v2 direction (not now):** `list[int]`/`list[float]` as (ptr,len) arrays,
  small-tuple `#[repr(C)]` returns, `Optional` as flag+value — and at that
  point evaluate replacing per-signature ctypes with a serialize-everything
  single `(ptr,len)→RustBuf` boundary (one fixed ABI for all signatures; for a
  *verification* pipeline the encode/decode cost is probably the right trade).
  The cliff (out of scope indefinitely): dicts, user classes, callables,
  impure functions.

## 10. What this buys later

- **C→Zig, Go→Rust, Rust→C:** any pair where both sides speak the C ABI rides
  `DifferentialOracle` unchanged — each is a `TargetLanguage`/`SourceLanguage`
  binding + recipes.
- **TS→Rust (or TS→TS):** a value-level `ReplayOracle` over a Node subprocess
  is the TS analog of §6.4 — the batch protocol and comparison predicates
  carry over wholesale.
- **The build-system rung** (the "pie in the sky" track): preprocessed-TU
  ingestion produces worklists that feed this same engine — the orchestration,
  oracles, and gate don't change.
