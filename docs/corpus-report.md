# Corpus robustness + correctness report (2026-07-19)

`cgir scan` run across **23 public repositories** spanning all five adapters,
via `benchmarks/corpus_scan.py`. Each repo is checked four ways, not just
"did it run":

1. **scan** — crash / timeout / success + wall time;
2. **extraction ratio** — independently tree-sitter-parse the source, count the
   function-like definitions cgir *should* extract, and report
   `extracted / present`. This denominator is what turns "1,513 components"
   into "1,513 of 2,552 (59%)" and makes under-extraction visible;
3. **determinism** — scan twice, require an identical component set;
4. **downstream** — run `stats` / `search` / `impact` on the real graph so the
   whole pipeline, not just ingest, is exercised.

## Headline: robust, deterministic, extraction ~80–99% on clean code

**23/23 scanned cleanly — 0 crashes, 0 timeouts, 0 non-deterministic, 0
downstream failures. 1.41M LOC → 45,120 components against 51,180
tree-sitter-counted definitions.** Largest were redis (207k LOC C, 15.7s),
sqlalchemy (247k LOC Python, 14.0s), django (165k, 15.4s); nothing timed out.

Extraction ratio by language (median [range]):

| language | median | range | reads as |
|---|---|---|---|
| go | 0.99 | 0.94–1.00 | essentially complete |
| c | 0.96 | 0.59–1.19 | complete except macro-dense outliers (below) |
| python | 0.87 | 0.83–0.95 | gap = nested/local `def`s cgir doesn't componentize |
| rust | 0.81 | 0.80–0.90 | gap = trait-impl / nested / macro-generated |
| typescript | 0.78 | 0.00–1.61 | noisy denominator (arrows) + the JS gap (below) |

Ratios above 1.0 (ky 1.61, tiny-AES-c 1.19) mean cgir legitimately extracts
things the ground-truth node set doesn't count (TS arrow functions bound to
variables; C functions under distinct `#if` branches), so the ratio is a
diagnostic, not a precise score. The healthy band is ~0.85–0.99; the two
values that fall out of it are real gaps.

## Finding 1 (real gap): JavaScript files are not ingested

**axios: 0 of 159 definitions (0.0).** `axios/lib` is 67 `.js` files, and the
TypeScript adapter declares `file_extensions = (".ts", ".tsx")` — so cgir
ingests nothing. The TS tree-sitter grammar is a superset of JS and parses it
fine (import resolution already handles `.js`), so this is a coverage choice,
not a parser limit. The old count-only harness showed "0 components" with no
denominator and no way to know it was wrong; the ground-truth check flags it
immediately.

*Proposed fix (adapter surface — needs sign-off):* add `.js` / `.mjs` / `.cjs`
to the TS adapter's `file_extensions` (`.jsx` needs the TSX grammar). Low risk;
lights up the large JS-only ecosystem.

## Finding 2 (real bug): functions buried in tree-sitter `ERROR` nodes

**stb: 1,513 of 2,552 (0.59).** Per-file, `stb_image.h` is now 219/221 (99%,
post-#ifdef-fix), but three files extract ~nothing: `stb_vorbis.c` 0/115,
`deprecated/stb_image.c` 0/159, `stb_truetype.h` 3/144.

Root cause: in these macro-dense files tree-sitter hits a parse error early and
wraps the entire remainder of the file in a single top-level `ERROR` node — the
real functions sit at `translation_unit > ERROR > preproc_ifdef >
function_definition`. `_iter_top_level` (the #ifdef fix) recurses through
preprocessor conditionals but not through `ERROR` nodes, so cgir sees only the
leading comments and extracts zero. tree-sitter's error recovery keeps the
buried function subtrees well-formed; cgir simply never descends to them.

*Proposed fix (C adapter, same class as the just-landed #ifdef fix):* add
`ERROR` to the transparent-recursion set in `_iter_top_level` (only known node
types are ever processed, so descending an ERROR wrapper is safe). Expected to
recover stb_vorbis / stb_truetype and any real macro-dense C that trips
tree-sitter mid-file. Redis (0.99), curl (0.92), jq (0.93) already scan
near-complete, so this is the tail, not the common case.

## Reproduce

```
python benchmarks/corpus_scan.py --out benchmarks/corpus-report.json
```

Per-repo data (extraction ratio, determinism, downstream, kinds) in
`benchmarks/corpus-report.json`.
