# Python→Rust addressable surface — a 26-repo sweep (2026-07-23)

`benchmarks/python_rust_sweep.py` runs the `python-rust` worklist over every
*pure* function in 26 real public Python libraries (web frameworks, heavily-typed
libs like pydantic/attrs/mypy/black, functional libs like toolz/more-itertools,
and string/parse utilities) and reports what's eligible for
`cgir rewrite --lang python-rust` and why the rest isn't. It doubles as a
stress test of the `ast`-based eligibility parser on real-world annotations.

## Headline: the v1 surface is small and honest — 0.87%

**231 eligible functions across 26 repos, out of 26,518 pure functions (0.87%).**
Zero crashes parsing 26k real functions. The eligible set is exactly what v1
targets — scalar + `str`/`bytes` **leaf** utilities:

| eligible return kind | count |
|---|---|
| `str` (RustBuf) | 137 |
| `bool` | 74 |
| `int` (i64) | 14 |
| `bytes` | 5 |
| `float` | 1 |

They read as a list of the string/path/encoding helpers you'd actually want in
Rust: `click.strip_ansi`, `click._posixify`, `click.unstyle`, `click.term_len`,
`requests.unicode_is_ascii`, `flask._path_is_ancestor`, `httpx._is_known_encoding`.

## Why the other 99% is out (the real story)

| reason | count | note |
|---|---|---|
| **method** (`self`/`cls`) | 16,433 | 62% of all pure functions — the dominant shape |
| non-scalar / unannotated param | 3,463 | containers (`list`/`dict`/`Sequence`), unions, `Optional`, bare params |
| default arguments | 2,661 | arity isn't fixed — a v2 candidate (capture sees the real arity) |
| `*args`/`**kwargs` | 2,404 | variadic ABI |
| keyword-only args | 525 | not passed positionally |
| async function | 370 | can't be a plain `extern "C"` fn |
| non-simple / missing / unsupported return | 365 | union/container/`Optional`, or no annotation |
| void / not-python / other | 63 | |
| unparseable / not-found (long tail) | 7 | 0.03% — nested funcs / span quirks |

The takeaway: v1's ceiling is set almost entirely by **methods** (structural) and
**container/complex-type params** (a v2 IR question — `list[int]` as `(ptr,len)`,
tuples as `#[repr(C)]` returns), not by the parser missing things. The eligible
leaves are a real, if narrow, slice — and each is cheap to rewrite and
mechanically verifiable.

## Bug found by the sweep

`parse_signature` matched only `ast.FunctionDef`, so **370 `async def` functions**
were reported as `function definition not found in source` — a misleading reason
that looked like a lookup bug. Fixed: match `ast.AsyncFunctionDef` too and reject
it cleanly as `async function not supported`. Regression-tested. This is exactly
why the sweep runs the parser over real code, not just the fixture.

## Capstone: a real function from a real library, rewritten and verified

`markupsafe._native._escape_inner` — the HTML escaper at the heart of a
foundational library — rewritten live by Haiku, one command:

```
cgir rewrite --lang python-rust --repo <markupsafe> --capture --live --apply
```

Captured **80,033 real calls (26 distinct inputs)** from markupsafe's own test
suite → Haiku wrote the Rust escaper (`&`→`&amp;`, `<`→`&lt;`, the exact `&#39;`
/ `&#34;` numeric entities) → replay-verified against those recorded inputs →
compiled + spliced in → **markupsafe's own 79-test suite passes with the Rust
escaper inside**, for **$0.003**.

Three more parser/oracle limitations the push surfaced and fixed on the way:
- **str subclasses.** markupsafe's tests pass a `str` subclass; type-exact
  validation rejected it. Relaxed str/bytes to `isinstance` (int/float/bool stay
  exact — that coercion is the real false-pass hazard; a behavior-changing str
  subclass would diverge and be caught as a mismatch).
- **trace dedup.** 80k calls / 26 distinct inputs — dedup by argument tuple; for
  a pure function the distinct inputs are the evidence and the dups just slow
  replay. `--min-traces` now counts distinct inputs.
- **`covered:true` default.** That static heuristic (direct test→fn calls only)
  excludes transitively-exercised leaves like `_escape_inner`; python-rust now
  defaults to `kind:pure` and lets dynamic capture + the min-traces floor gate.

## Live battery across real libraries — the verifier is sound

Beyond markupsafe, live rewrites (isolated venv per repo: `uv venv` + editable
`cgir[llm]` + editable target + its test plugins) on more libraries. The
rejections are the point — they show the pipeline refuses to apply a rewrite it
can't verify, on real code:

| library | function | result |
|---|---|---|
| markupsafe | `_escape_inner` (HTML escape) | **solved**, applied, its 79 tests pass, $0.003 |
| semver | `_increment_string` (increment trailing digits) | **solved** (escalated, $0.057), applied, its tests pass |
| semver | `_increment_prerelease` | **rejected** — model couldn't see `Version._LAST_PRERELEASE` (a class-level regex not in the fn source), tried the `regex` crate (rustc fails), then guessed wrong; replay caught it: `_increment_prerelease('rc1')` expected `'rc1.0'`, got `'rc2'` |
| semver | `compare` | **excluded** — a recorded result is `None` despite the `-> int` annotation; a Rust `-> i64` can't return `None`, so it's unverifiable |

Two distinct soundness wins on real code: replay catching a **plausible-but-wrong**
translation with a concrete counterexample, and validation refusing a function
whose **annotation doesn't match its recorded behavior**. Neither was a false pass.
`_increment_prerelease` also names the next real limitation — a function that
references a module/class constant (here a compiled regex) it doesn't define is
the Python analog of C→Rust's invisible-macro problem, which c-rust solved with
compiler-probed context; the python analog (inject referenced module constants
into the prompt) is a v2 candidate.

## Operational reality: eligible ≠ rewritable

The sweep's static eligibility is a ceiling; *rewriting* one live needs three
things to line up, and real repos routinely miss the last two:

1. **eligible** (static — 0.87% of pure functions);
2. **exercised** — the function must actually be *called* during the test run to
   record traces. Many eligible leaf helpers aren't (packaging: 11 eligible, ~0
   traces — its tests exercise higher-level APIs that don't route through them);
3. **a runnable test environment** — the repo's own pytest must run: src-layout
   repos must be captured from the repo *root* (tests live outside `src/`), and a
   single missing plugin (`pytest-cov` in semver's `addopts`) or one collection
   error (rich) yields zero traces. Isolated-venv-per-repo with the repo's test
   deps is the working pattern.

This is why the honest live yield is small — but each success is a real function
in a real library, verified against that library's own recorded inputs, with its
own test suite green over the Rust.

## Does it make Python faster? Measured — and the FFI is the ceiling

The other half of "worth rewriting" is: is the Rust *faster*? Benchmarking the
functions we actually rewrote — original Python vs the ctypes wrapper `--apply`
emits — gives a clear crossover (`benchmarks/python_rust_speedup.py` reproduces
it; markupsafe escape shown):

| input | Python | Rust+ctypes | speedup |
|---|---|---|---|
| empty | 78 ns | 463 ns | **0.17×** |
| 20 chars, no specials | 138 ns | 777 ns | **0.18×** |
| 20 chars, w/ specials | 278 ns | 868 ns | **0.32×** |
| 200 chars, mixed | 1823 ns | 1201 ns | 1.52× |
| 5 KB, no specials | 8072 ns | 7281 ns | 1.11× |
| 5 KB, many specials | 47396 ns | 9078 ns | **5.22×** |

semver `_increment_string` on typical version strings: **0.67–0.88×** (slower).

Two facts:
- **The fixed ctypes tax is ~390 ns/call** (the empty-input delta). Every call
  pays it: encode UTF-8 → marshal → call → `string_at` → decode → free.
- **The eligible surface is dominated by small string/scalar leaves** — exactly
  the functions whose compute is *under* 390 ns, where Python's builtins are
  already C-level. So most of what v1 *can* rewrite, it makes **slower**. Rust
  wins only once per-call compute clears the tax (large inputs with real work).

The bottleneck is the FFI *mechanism*, not Rust — the Rust escaper is 5.2× on a
5 KB payload. So the pipeline now has **both apply targets**: `--apply` verifies
and ships via ctypes (needs only `rustc`); `--apply --pyo3` builds the *same
verified* winners into a native PyO3 extension (`cgir/ffi/targets/pyo3.py` — the
`extern "C"` functions kept verbatim, thin `#[pyfunction]` shims over them; needs
`cargo`). Measured three ways:

| input | Python | ctypes | **PyO3** | ctypes → PyO3 vs Python |
|---|---|---|---|---|
| empty | 78 ns | 483 ns | 51 ns | 0.16× → **1.54×** |
| 20 ch, w/ specials | 281 ns | 876 ns | 203 ns | 0.32× → **1.38×** |
| 200 ch, mixed | 1851 ns | 1206 ns | 548 ns | 1.54× → **3.38×** |
| 5 KB, many specials | 47.8 µs | 9.2 µs | 8.5 µs | 5.22× → **5.60×** |

**PyO3's per-call tax is ≈ 0** (within noise of a plain Python call, vs ctypes'
~400 ns). It flips the small-string cases from *slower* to *faster* — the
crossover drops from ~200 chars to ~20 — while shipping the exact bytes that
passed replay-verification. The clean split, now real: **verify with ctypes**
(sound, `rustc`-only) and **ship with PyO3** (`cargo`, ~0 overhead).

**What this means for the vision.** It sharpens "eligible ≠ worth it" into a
number: of the 0.87% eligible, the subset where a Rust rewrite is *actually
faster today (ctypes)* is smaller still — compute-bound functions on large
inputs. The path to a broad, worthwhile speedup surface is not more model
quality (the translations are correct) but (1) the IR-shape unlocks that raise
0.87% and (2) a lower-overhead apply target (PyO3) that lowers the break-even.

## Reproduce

```
python benchmarks/python_rust_sweep.py --out benchmarks/python-rust-sweep.json
```

Per-repo data in `benchmarks/python-rust-sweep.json`. Note: eligibility is a
static, coverage-independent property; actually *rewriting* an eligible function
additionally needs test coverage to capture traces (a separate, environment-gated
step — each repo's own test suite must run to record real I/O).
