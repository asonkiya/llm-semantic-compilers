# Corpus robustness report (2026-07-19)

`cgir scan` run across 15 public repositories spanning all five adapters,
via `benchmarks/corpus_scan.py` (shallow clone → scan under a 420s timeout →
record success/crash/timeout + stats). Scanning is fully static
(tree-sitter, no repo dependencies), so no per-repo setup was needed.

## Headline: robust

**15/15 scanned cleanly — zero crashes, zero timeouts, zero clone failures.
554,790 LOC → 10,127 components.** The slowest scan was curl (173k LOC of C)
at 3.3s; nothing needed more than a few seconds.

| repo | lang | KLOC | components | comps/KLOC | pure% | scan |
|---|---|---|---|---|---|---|
| flask | python | 10 | 332 | 34.9 | 67.8 | 0.7s |
| requests | python | 6 | 237 | 37.1 | 50.2 | 0.6s |
| click | python | 13 | 490 | 39.2 | 71.0 | 0.9s |
| httpx | python | 9 | 424 | 48.0 | 71.2 | 0.8s |
| rich | python | 39 | 821 | 21.3 | 72.5 | 1.7s |
| zod | typescript | 74 | 933 | 12.6 | 90.4 | 2.2s |
| ky | typescript | 4 | 87 | 21.7 | 94.3 | 0.4s |
| cobra | go | 17 | 588 | 35.1 | 84.5 | 1.1s |
| gin | go | 24 | 1318 | 54.7 | 58.0 | 1.9s |
| ripgrep | rust | 49 | 2272 | 46.7 | 89.7 | 2.5s |
| clap | rust | 29 | 1057 | 36.5 | 92.6 | 1.3s |
| tiny-AES-c | c | 1 | 24 | 24.5 | 50.0 | 0.3s |
| kilo | c | 1 | 36 | 27.5 | 22.2 | 0.3s |
| stb | c | 107 | 311 | **2.9** | 63.7 | 1.8s |
| curl | c | 174 | 1197 | **6.9** | 56.6 | 3.3s |

Purity distributions pass a sanity check per language: type-heavy TS ~90%+,
builder/derive-heavy Rust ~90%, I/O-heavy C editors low (kilo 22%), and the
C libraries mid-range.

## Finding 1 (real bug): the C adapter skips functions inside `#ifdef`

**stb and curl stand out at 2.9 / 6.9 components per KLOC — far below the C
median (15.7).** Confirmed on `stb_image.h`: tree-sitter finds **221
`function_definition` nodes; cgir extracts only 36 (16%).**

Root cause: `CAdapter` iterates `root.named_children` — the *direct* children
of the translation unit. tree-sitter-c nests conditionally-compiled top-level
definitions inside `preproc_ifdef` / `preproc_if` nodes, which are children of
the translation unit, so the function definitions inside them (children of the
preproc node, not of the root) are never visited. stb's single-header pattern
wraps its entire implementation in `#ifdef STB_IMAGE_IMPLEMENTATION`, so ~84%
of it is invisible; curl's platform/feature `#ifdef`s cause the milder version.

**Fixed (2026-07-19, `c.py:_iter_top_level`):** treat `preproc_ifdef`/
`preproc_if`/`preproc_elif`/`preproc_else` as transparent — recurse into them
when collecting `function_definition` / `struct_specifier` / `type_definition`
/ `preproc_include`, deduping symbols that appear under mutually-exclusive
branches (an x86 vs portable variant is one component). After the fix:

| repo | before | after | comps/KLOC |
|---|---|---|---|
| stb | 311 | **1,513** | 2.9 → 14.2 |
| curl | 1,197 | **3,511** | 6.9 → 20.2 |
| stb_image.h alone | 36 / 221 | **219 / 221** | 16% → 99% |

**The correction that matters most:** SQLite was *not* immune, as first
assumed here. Re-scanned, the amalgamation goes **2,663 → 5,067 components
(583 → 1,120 pure)** — its `#ifdef SQLITE_ENABLE_*` / `SQLITE_OMIT_*` feature
guards hid ~47% of its functions. The amalgamation flattens `#include`, not
feature `#ifdef`s. So the rung-1/rung-4 SQLite figures elsewhere in the docs
were computed on a ~half-complete graph; the real candidate pool is roughly
double. This is the corpus test earning its keep — it found a bug that had
been silently shrinking even our flagship validation.

## Finding 2 (not a defect): call resolution is in-repo by design

`calls/component` varies widely (clap 0.16, curl 1.67) and looks alarming
until you check the denominator: **flask resolves 61 of 1,225 call sites (5%);
ripgrep 751 of 12,760 (6%)** — nearly identical rates across languages. cgir
resolves only calls whose target is *defined in the repo*; the other ~94% are
stdlib/third-party/method calls it deliberately does not chase (dynamic
dispatch is a documented precision limit). So the spread reflects how much
internal calling and macro-generation each codebase has, not adapter quality.
No action.

## Reproduce

```
python benchmarks/corpus_scan.py --out benchmarks/corpus-report.json
```

Full per-repo data (kinds, languages, node/edge counts) is in
`benchmarks/corpus-report.json`.
