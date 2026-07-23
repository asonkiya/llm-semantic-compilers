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

## Reproduce

```
python benchmarks/python_rust_sweep.py --out benchmarks/python-rust-sweep.json
```

Per-repo data in `benchmarks/python-rust-sweep.json`. Note: eligibility is a
static, coverage-independent property; actually *rewriting* an eligible function
additionally needs test coverage to capture traces (a separate, environment-gated
step — each repo's own test suite must run to record real I/O).
