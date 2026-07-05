# Feature research: everything a consumer may want from CGIR

Written 2026-07-05 overnight. Companion to [`strategy.md`](./strategy.md)
(landscape + positioning). This is the exhaustive catalog: for every
feature a plausible user could want, what it is, who wants it, what
already exists elsewhere, how it would be implemented on CGIR's actual
architecture, effort (S ≤ 1 day, M ≤ 1 week, L = multi-week), and a
verdict. Read the verdicts column first, then dive into whatever you
disagree with.

Personas used throughout:
- **A** — agent operators (people running Claude Code/Cursor/Codex on repos)
- **R** — reviewers / team leads gating AI-written PRs
- **D** — individual developers understanding/refactoring their own code
- **M** — migration & modernization teams
- **S** — security / audit
- **P** — platform / architecture owners of large codebases

---

## 1. Comprehension & onboarding

### 1.1 Interactive visualization (SHIPPED, keep polishing)
Four views, sliders, tracing, entrypoint rings. **Remaining consumer asks:**
- **Export to SVG/PNG** (D, P — for docs/slides). *Impl:* canvas → `canvas.toBlob` PNG is ~20 lines; SVG needs a parallel render path (M). Verdict: PNG now (S), SVG later.
- **Shareable state** (P — "look at this node"): encode view/selection in URL hash. *Impl:* serialize `{view, selected, camera}` to `location.hash` on change, restore on load (S). Verdict: build.
- **Minimap** (D — orientation at 441+ nodes): second small canvas drawing node dots + viewport rect (S/M). Verdict: build when someone asks.
- **Diff overlay** (R — "what did this PR change, visually"): feed two indexes, color nodes added/removed/contract-changed. *Impl:* `_build_data(specs, old_specs)` marks per-node `status`; JS tints. Pairs with `cgir diff` (M). Verdict: high-leverage with the CI story; build in Action milestone.

### 1.2 Generated architecture document (D, P, M)
`cgir document` → `ARCHITECTURE.md`: entrypoint table, module summary
(files view data as prose + mermaid), data model (constructed types +
fields), effect map, hotspots. *Impl:* pure renderer over specs + stats +
flow — same pattern as `report/stats.py`; mermaid module diagram already
exists. No LLM required for v1; optional `--live` pass to prose-polish.
Effort: M. Verdict: **build** — cheap, demoable, every repo wants one.

### 1.3 "Explain this repo / component" narratives (A, D)
LLM-written walkthroughs seeded by pack/stats/flow. *Impl:* prompt
templates over existing bundles via the injectable-generator seam.
Effort: S given seam. Verdict: defer — agents already do this well when
handed the pack; the pack is the product.

### 1.4 Dead code & orphan detection (D, P)
Components with no callers, no entrypoint, no test linkage ≈ dead.
Comparable: vulture (regex/AST heuristics, no entrypoint awareness).
*Impl:* pure-graph query — in-degree 0 ∧ entrypoint None ∧ not dunder ∧
not test. CGIR's entrypoint recognition makes this *more precise than
vulture* for framework apps. Caveats: dynamic dispatch false positives —
report as "likely unreferenced" with the honesty markers. Effort: S.
Verdict: **build** — `cgir stats --dead` or a stats section; instant value.

### 1.5 Health metrics over time (P)
Purity ratio, effect counts, cycle counts, dead count, tracked per commit.
*Impl:* `cgir stats --json` snapshots + a tiny trend renderer; or leave to
the user's dashboards. Effort: S (emit), M (render). Verdict: emit-only;
let CI artifacts + the diff gate carry the story.

---

## 2. CI gates & governance (the strategic wedge)

### 2.1 GitHub Action (`cgir-action`) (R, P) — **flagship**
Scan base ref + head ref, `cgir diff --fail-on ...`, post a PR comment
with the drift table (and 1.1's diff-overlay viz artifact). *Impl:*
composite action: checkout base → scan → checkout head → scan → diff
--json → comment via `gh api`. All CLI pieces exist; the work is action
YAML, comment formatting, and docs. Effort: M. Verdict: **build first** —
it is the distribution vehicle. Free, deterministic, zero per-seat cost
against Greptile's $20–30/user LLM judgments.

### 2.2 Architecture rules / boundaries (P, R)
Comparables: [Tach](https://github.com/tach-org/tach) (module dependency
enforcement, Rust, popular), [import-linter](https://import-linter.readthedocs.io/)
(layer contracts). Both operate on *imports only*. CGIR can enforce
**semantic** rules no import linter can express:
- `pure modules must not call effect_adapters` (kind-aware)
- `only adapters/ may have net` (effect-aware)
- `components reachable from HTTP entrypoints must not read os.environ`
  (reachability-aware)
*Impl:* `cgir.toml` rules section; `cgir lint` evaluates over specs +
CALLS closure. Each rule type is a pure function over the index; ~5 rule
primitives (kind-call, effect-location, reachability, layer, cycle).
Effort: M. Verdict: **build second** — it converts the taxonomy into
enforceable policy and is the "ruff for architecture" claim made literal.
Position *with* Tach compatibility, not against (import rules → keep Tach;
semantic rules → CGIR).

### 2.3 Entrypoint surface tracking (R, S)
"New route `POST /api/x` appeared in this PR." Already derivable from
`cgir diff` (entrypoint is a contract field — added components carry it).
*Impl:* add `entrypoint-change` + `entrypoint-added` fail rules and a
surfaced section in the diff output. Effort: S. Verdict: **build** with 2.1.

### 2.4 Config/env surface (S, P)
Catalog `os.environ` / `os.getenv` / `Settings` field reads per component.
*Impl:* effects-style lexical pass (receiver `os.environ`, `getenv`) →
new spec list `reads_env`; schema add. Effort: S/M. Verdict: build when a
user asks; slots cleanly into the effects walker.

---

## 3. Agent tooling

### 3.1 Contract-enriched pack (A) — **evidence-ranked #1**
The experiment showed failures are missing *shapes*, not algorithms.
Enrichments in measured priority order:
1. **Type closure**: for every type named in signature/outputs/constructs,
   include the class definition source (Class nodes have spans; read via
   SourceCache). Include dataclass/pydantic fields. Effort: M.
2. **Docstring + raises**: ingester stores docstring (first string in
   body) and raised exception names (CFG walk already sees
   raise_statement — capture the callee name). Spec fields `doc`,
   `raises`. Effort: S.
3. **Test linkage** (see 3.4) → include linked test source in pack.
   Effort: S once linkage exists.
4. **Module-constant closure**: module-level assignments referenced by the
   component (names in reads not bound locally/params/imports) — include
   their source lines. Effort: M.
Then **re-run the benchmark**; target pack ≥ 8/12 at <800 avg tokens.
Verdict: **build immediately after the Action** — it's the difference
between a pack that names the world and one that defines it.

### 3.2 `cgir verify` (A, R) — the trust loop
`cgir verify <id> --candidate file.py [--repo]`: splice into shadow copy
(machinery written and 12/12-validated in the experiment harness),
re-scan, contract-diff vs the old spec, run linked tests, emit verdict
JSON. Expose as MCP tool so agents self-check before proposing edits.
Effort: M (mostly moving harness code into `cgir/verify.py` + tests).
Verdict: **build** — no shipped competitor; the experiment already proved
the mechanism.

### 3.3 Incremental re-index / watch mode (A, P)
Hash files; cache per-file subgraphs; on change re-ingest changed files,
re-run symbol/call/effect passes only for changed modules + dependents
(reverse CALLS/IMPORTS closure). Comparables: every competitor claims
"instant"; agents need fresh indexes after each edit. *Impl:* per-file
node/edge partitioning already implicit (ids carry paths/qualnames);
store file→node-ids map + content hash in the index; `cgir scan --update`.
Cross-file analyses (symbols/calls/effects closure) are the subtlety —
re-run globally but they're graph-cheap; only parsing is expensive and
that's per-file. Effort: M/L. Verdict: build after verify — the agent
loop (edit → verify) makes freshness mandatory.

### 3.4 Test linkage (A, R, D)
Map Test components → components they call. *Impl:* ingest `tests/` as
components (already happens), CALLS edges already resolve into source when
imports do; add `covered_by` computed field (reverse CALLS from test-file
components) on specs + `untested effectful components` stats section. The
grep fallback from the harness covers dynamic cases. Effort: S/M.
Verdict: **build** — feeds pack, verify, and the risk report.

### 3.5 Semantic/vector search (A)
Competitors bundle embeddings. *Impl:* optional extra, embed spec
summaries not raw code. Effort: M + infra. Verdict: **skip for now** —
`search` over ids/entrypoints/effects + agent grep covers it; embeddings
add deps, cost, and no differentiation.

### 3.6 Rewrite-readiness benchmark (A, P; also marketing)
Productize the harness: `cgir bench --sample 20 --model ...` → readiness
score + failure taxonomy. Effort: M. Verdict: build after 3.1 so the
number is worth publishing.

---

## 4. Refactoring & transformation

### 4.1 `cgir decompose` (D, M) — long-term flagship
PDG-sliced functional-core/imperative-shell proposals (detailed in
strategy.md). *Impl sketch:* within an effect_adapter, partition CFG nodes
into effect-bearing (db/io tags, mutator calls on params) vs pure; find
maximal pure regions via DEPENDS_ON/FLOWS_TO closure; a region with
single-entry data deps (params ∪ region-external reads) and single-exit
(vars read after region) is extractable → emit suggestion with synthesized
signature. Verify with 3.2 after the LLM performs the edit. Comparables:
extract-method research is active (arXiv), nothing shipped does
effect-aware core extraction. Effort: L. Risks: precision of mutates/
aliasing; mitigate by suggesting only high-confidence regions. Verdict:
the differentiator; build after the loop (2.1→3.2) exists to verify it.

### 4.2 Extract-selection safety check (D)
Given file:lines, report what flows in/out (params needed, values
returned, effects inside) — "can I extract this?" *Impl:* same PDG region
math as 4.1 on an arbitrary span; simpler because the user picks the
region. Effort: M (after 4.1 groundwork). Verdict: nice stepping stone to
4.1; possibly ship first as `cgir extract-check`.

### 4.3 Contract-checked codemods (M, P)
Comparables: [ast-grep](https://ast-grep.github.io/) (fast structural
rewrite), [codemod](https://github.com/codemod/codemod) (orchestration).
They transform; nothing verifies semantics. *Impl:* don't build a rewrite
engine — **wrap**: `cgir codemod --run "ast-grep ..." --verify` = snapshot
index → run tool → rescan → diff → report contract drift. Effort: S/M.
Verdict: build the wrapper, never the engine.

### 4.4 Cross-language migration packs (M)
The original regeneration vision (Python→TS per component). Needs: TS
ingester (6.1) + enriched pack (3.1) + verify (3.2) on the TS side.
Verdict: defer until those exist; then it's mostly prompt + pipeline glue.

### 4.5 Test scaffolding from specs (D)
Generate pytest skeletons: params from inputs/types, fixtures hinted by
effects (db → session fixture), assertions stubbed from outputs.
*Impl:* renderer over specs; optional LLM fill via generator seam.
Effort: S/M. Verdict: build after test linkage — targets the "untested
effectful" list from 3.4, which is where scaffolds are wanted.

---

## 5. Security & audit

### 5.1 Entrypoint→sink reachability report (S, R)
"Which HTTP routes reach raw SQL / subprocess / fs writes?" Comparables:
[Pysa](https://pyre-check.org/docs/pysa-basics/) and
[Semgrep taint mode](https://semgrep.dev/docs/writing-rules/data-flow/taint-mode)
do real taint analysis. CGIR should NOT compete on taint precision —
but a *reachability × effects* report (CALLS closure from entrypoints to
components carrying db/net/fs) is one BFS over existing data and catches
the architecture-level question ("this route can touch the filesystem —
should it?"). *Impl:* `cgir reach --from-entrypoints --effect fs`;
param_flow adds "with request data" coloring (may-flow, honest). Effort:
S/M. Verdict: **build the reachability report**, explicitly *not* branded
as taint analysis; refer users to Pysa/Semgrep for injection-hunting.

### 5.2 Third-party surface (S, P)
Which components call which external packages (unresolved import targets
aggregated). *Impl:* stats over import bindings + effect tags. Effort: S.
Verdict: build as a stats section when asked.

---

## 6. Languages & scale

### 6.1 TypeScript ingester (everyone) — **the coverage unlock**
tree-sitter-typescript; map: modules/exports (incl. `export default`,
barrel files), functions/arrow-consts/methods, imports (ESM paths ↔
qualname mapping), constructors (`new X`), entrypoints (Express/Fastify
routes, React components?, CLI). Analyses port via the grammar-agnostic
seams: CFG builder needs a TS statement dispatch (if/for/while/try/
switch); reads/writes extraction per TS grammar; effects tables for
fetch/axios/fs/process. Reality check: `this`-heavy OO + closures reduce
precision vs Python; async everywhere. Effort: **L (the big one)** —
suggest milestone split: L1 ingest+symbols+calls (usable stats/viz/MCP),
L2 CFG/PDG, L3 effects/entrypoints. Verdict: schedule deliberately after
the Action + enrichment ship; it doubles the audience and covers your own
repos.

### 6.2 Resolution upgrade via SCIP/pyright (P, correctness ceiling)
Our homegrown resolution misses methods/dynamic dispatch — the documented
precision ceiling. [scip-python](https://github.com/sourcegraph/scip-python)
(pyright-based) emits compiler-accurate symbol references in a stable
protobuf format. *Impl:* optional `--resolver scip`: run scip-python,
parse the index, use its references to build CALLS (keep tree-sitter for
structure/CFG). This would collapse most "unresolved" honesty markers and
make verify/decompose trustworthy on OO code. Effort: L (but bounded —
format is documented; no analysis to write). Verdict: **the single
highest-leverage correctness investment**; do it when verify's users hit
the resolution ceiling. Same trick later for TS via scip-typescript.

### 6.3 Monorepo scale (P)
JSON index will strain ~50k+ components. *Impl:* SQLite index format
(nodes/edges/specs tables) behind the existing read/write seams; keeps
MCP/CLI unchanged. Effort: M/L. Verdict: defer until someone brings a
monorepo; design nothing that blocks it (ids are stable strings — fine).

---

## 7. Distribution & DX

- **PyPI release** (`pip install cgir`): package is ready; add versioning,
  changelog, `cgir --version`. Effort: S. Verdict: **do with the Action**.
- **pre-commit hook**: `cgir diff` against HEAD index. Effort: S. Verdict: build.
- **VS Code extension** (viz panel + component hover): defer; MCP covers
  agent IDEs already. Effort: L.
- **Docs site**: mkdocs over existing docs/. Effort: S/M. Verdict: with PyPI.
- **Index format stability**: version field in index + schema; needed
  before Action (base/head produced by different cgir versions). Effort: S.
  Verdict: do with the Action.

---

## Decision matrix (build order)

| # | Feature | Persona | Effort | Why now |
|---|---|---|---|---|
| 1 | GitHub Action + PR comment + entrypoint rules + index versioning + PyPI | R,P | M | Distribution vehicle; empty niche |
| 2 | Pack enrichment (types, docstrings, raises, constants) + test linkage | A | M | Evidence-ranked from the experiment |
| 3 | `cgir verify` (+ MCP tool) | A,R | M | Trust loop; harness 80% done |
| 4 | Re-run benchmark → publish readiness score | mkt | S | The credibility artifact |
| 5 | Architecture rules (`cgir lint`) | P,R | M | "Ruff for architecture" literalized |
| 6 | Dead-code report + reachability report + arch doc generator | D,S,P | S–M | Cheap wins riding existing data |
| 7 | Incremental re-index | A | M/L | Agent loop freshness |
| 8 | TypeScript L1 (ingest/symbols/calls) | all | L | Audience doubling |
| 9 | SCIP resolver option | P | L | Correctness ceiling lift |
| 10 | `cgir decompose` (via extract-check) | D,M | L | The flagship, once verifiable |

Explicit skips: vector search, VS Code extension (for now), hosted
dashboard, taint analysis (refer to Pysa/Semgrep), building a rewrite
engine (wrap ast-grep/codemod), Neo4j/Joern/CodeQL bridges (until an
enterprise user exists), LLM regeneration as a product (agents generate;
CGIR checks).

## Sources
- Boundaries: https://github.com/tach-org/tach · https://import-linter.readthedocs.io/
- Resolution: https://sourcegraph.com/blog/scip-python · https://github.com/scip-code/scip · https://scip-code.org/
- Codemods: https://ast-grep.github.io/advanced/tool-comparison.html · https://github.com/codemod/codemod · https://semgrep.dev/blog/2022/autofixing-code-with-semgrep/
- Taint: https://pyre-check.org/docs/pysa-basics/ · https://semgrep.dev/docs/writing-rules/data-flow/taint-mode · https://github.com/laugiov/code-safety
- Plus strategy.md sources (graph/MCP competitors, packing, review market).
