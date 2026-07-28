# Roadmap

**North star: [`vision-rewrite.md`](./vision-rewrite.md)** — rewriting massive codebases with simple models; the contract layer is its verifier. That north star is now the product's centre of gravity: the rewrite orchestrator (`cgir rewrite`), the C→Rust pipeline (`--lang c-rust`, `--lift`, whole-program gate), the capture/replay oracle, and the behavioral-diff gate (`cgir verify-diff`) all ship on PyPI as **0.6.3**. `plan-0.2-0.4.md` and `plan-0.3.md` are fully landed.

Forward-looking sequencing. The grouping mirrors `Code-IR.md` §Architecture: **P0** is "you can produce a `ComponentSpec`," **P1** is "you can trust it," **P2** is "you can scale it." P0 and P1 are complete; P2 is the only tier with open milestone tags (`grep -rn "milestone:" src/` → `P2-neo4j`, `P2-joern-bridge`, `P2-codeql-bridge`). Within each tier the order below reflects current dependencies, not strict chronology — feel free to interleave when dependencies allow.

## Done

**Sprint 0 (P0):**
- Tree-sitter ingest → structural skeleton (`Repository / File / Module / Function / Method / Class / Parameter / Import`).
- Symbol resolution + cross-file imports.
- Call graph (`CALLS` edges).
- Effects classifier (`io`, `raise`, transitive `calls_effectful`).
- Purity scorer (1.0 / 0.7 / 0.0).
- Slicer + ComponentSpec JSON export + trace map + CLI.

**Sprint 1 (P1):**
- Intra-procedural CFG: `Statement` / `Assignment` / `Branch` / `Loop` / `Return` nodes connected by `CONTROLS` edges; Function is the entry, Return is a sink; loops emit back-edges; `if` without `else` falls through. Wired between `call_graph` and `effects` in the pipeline.

**Sprint 2 (P1):**
- Assignment `writes` attribute recorded by CFG (LHS identifier names, recursing through tuple/list patterns; subscript/attribute LHS skipped).
- Reaching-definitions worklist analysis (`analyses/reaching_defs.py`): forward may-analysis over `CONTROLS` edges, with parameters as initial defs and `Assignment.attrs["writes"]` driving gen/kill. First **pure-graph** analysis — does not take `repo_path`, does not re-parse source.
- First opportunistic step on the grammar-agnostic core refactor: extracted duplicated `_parser` / `_locate_function` from `call_graph`, `effects`, `cfg` into `analyses/_python_ast.py`.

**Sprint 3 (P1):**
- CFG extended with per-node `reads` (per-stmt sub-expression: RHS / condition / iterable / returned value), `controlled_by` (id of immediately enclosing Branch/Loop), and Assignment `mutates` (attribute/subscript LHS base names).
- PDG (`analyses/pdg.py`): emits `FLOWS_TO` (data dependence, gated by reaching-defs) and `DEPENDS_ON` (control dependence from `controlled_by`). Second pure-graph analysis.
- Slicer recognizes `state_transformer` when any Assignment child has non-empty `mutates`.
- Reaching-defs and PDG now wired into the CLI scan pipeline.

**P1 complete (Sprints 4–19):** every `ComponentSpec` field has a real classifier; live regeneration (`cgir regenerate --live`) and the FastAPI surface both ship. Milestones — the precision fixes (`with`/`try`/`match`, augmented assignment, mutator tables), the extended effects taxonomy (`net`/`fs`/`nondeterm`/`db`, confidence tiers), visualization (`cgir viz`, GraphML, mermaid), tracing/impact/flow, the diff + pin + drift gates, the pre-commit hook + GitHub Action, `cgir stats`/`search`/`pack`, entrypoint recognition, coverage-grounded test linkage, the LSP server, and the MCP server — are enumerated in [`status.md`](./status.md) ("Recent milestones" + the feature matrix).

**Multi-language & adapter layer:** the grammar-agnostic core refactor landed. A `LanguageAdapter` seam (`languages/`) with a plugin registry (`cgir.languages` entry points) now backs first-class **TypeScript**, **Go**, **Rust**, and **C** adapters — Python's hardcoded tree-sitter node types no longer leak into the passes. An adapter-authoring guide (`docs/writing-an-adapter.md`) was validated by a docs-only agent writing the Rust adapter from scratch.

**The rewrite era (0.4 → 0.6.3, shipped on PyPI):**
- `cgir decompose` — PDG-sliced functional-core/imperative-shell suggestions.
- `cgir verify` — splice → rescan → contract-diff → tests, exposed over CLI + MCP.
- `cgir rewrite` — the orchestrator: query worklist → k cheap candidates → contract verify → shadow tests → escalation → resumable ledger, budget cap, `--apply` + final gate. Rewrite pairs are a plug-and-play registry (like the language layer).
- **C→Rust** (`--lang c-rust`): pure C leaves → cheap-model Rust → rustc → adapter contract-scan → differential vs the compiled C; `--non-leaf` (dependency-ordered), `--apply --link-out` link-back, and a `--gate-build`/`--gate-run` whole-program gate that keeps only byte-identical-to-stock winners.
- **C lifter** (`--lift <fn>`): tree-shake a function + its file-scope tables/typedefs/helpers out of a big single file into a standalone TU and rewrite it — one command, no pre-existing index.
- **Capture/replay oracle** (`--oracle replay`): trace real I/O from the test run, replay recorded inputs per candidate.
- **`cgir verify-diff`** — a behavior-preservation gate for AI-authored changes: record each changed function's real inputs from the suite, replay OLD vs NEW (with optional `--fuzz`), report preserving / diverged / unverified, exit non-zero on divergence. Python via pytest, JS/TS via `node --test`/jest. This is the contract layer applied to *any* edit, not just a full rewrite.

## P1 — Trust & explainability

The theme: make every `ComponentSpec` defensible. Today a function is "pure" if we couldn't *see* an effect; P1 makes that claim flow-sensitive, traceable to specific lines, and serveable over HTTP.

| # | Milestone tag | Why this comes first | Notes |
|---|---|---|---|
| ~~1~~ | ~~`P1-cfg`~~ | **Done (Sprint 1).** Unblocks 2 and 3. | `src/cgir/analyses/cfg.py`; 11 tests (later +4 for `writes` attr) in `tests/unit/test_cfg.py`. |
| ~~2~~ | ~~`P1-reaching-defs`~~ | **Done (Sprint 2).** Unblocks 3 and `WRITES`/`MUTATES` edges. | `src/cgir/analyses/reaching_defs.py`; 9 tests in `tests/unit/test_reaching_defs.py`. Pure-graph; wired into CLI in Sprint 3 alongside PDG. |
| ~~3~~ | ~~`P1-pdg`~~ | **Done (Sprint 3).** `FLOWS_TO` (data dep) + `DEPENDS_ON` (control dep). Lights up `state_transformer` in the slicer. | `src/cgir/analyses/pdg.py`; 10 tests in `tests/unit/test_pdg.py`. CFG extended with `reads`/`mutates`/`controlled_by` attrs to drive it. |
| 4 | ~~Extended effects taxonomy (`net`, `fs`, `nondeterm`)~~ **done (Sprint 5)** | Dropped into `effects.DIRECT_EFFECT_TAGS` with lexical, alias-aware matching. | Landed for `requests`/`urllib`/`socket`/`os`/`pathlib`/`time`/`random`/`datetime`; later extended with `db` and confidence tiers. |
| 5 | ~~Statement-granularity trace map~~ **done** | `trace_map.py` resolves `path:line` against the CFG. | Refines `cgir trace` output. |
| 6 | ~~`P1-regenerate`~~ **done (Sprint 9)** | Turn the prompt-pack into a real Anthropic SDK call. Add prompt caching from day one. | Landed: injectable generator seam; `anthropic_generator()` behind the `cgir[llm]` extra with cache_control on the system prompt. Compile/test round-trip verification before tagging `REGENERATED_AS` is still future work. |
| 7 | ~~`P1-api`~~ **done (Sprint 8)** | Replace 501 stubs in `api/server.py` with the real endpoints backed by the same passes the CLI uses. | Landed: `cgir/pipeline.py:scan_repo` is the single driver; CLI and FastAPI are thin surfaces over it. |

**P1 is done.** Every `ComponentSpec` field has a real classifier behind it (no `PLACEHOLDER_SCORE` defaults firing in practice), `cgir regenerate --live` drives a real Anthropic call, and the FastAPI surface mirrors the CLI.

## P2 — Scale

The theme: stop holding the graph in process memory, and accept secondary analyzers (Joern, CodeQL) for the cases where Tree-sitter alone is too shallow. **These three tags are the entire remaining backlog** — everything above this line has shipped.

| # | Milestone tag | Why | Notes |
|---|---|---|---|
| 1 | `P2-joern-bridge` | CPG-style overlays give us real interprocedural data-flow without re-implementing it. | Stub at `sources/joern_source.py`. Implement as a `GraphSource` that shells out to Joern's CLI and normalizes its CPG into our `Node`/`Edge` vocabulary. |
| 2 | `P2-codeql-bridge` | Secondary analyzer + export bridge. Useful for differential testing against Joern. | Stub at `sources/codeql_source.py`. Same pattern as Joern: shell out, normalize. |
| 3 | ~~`P2-graphml`~~ **done (Sprint 6)** | Cheap export for Gephi / yEd / Neo4j importers. | Landed in `export/graphml.py` — flattens attrs to GraphML-safe scalars. `cgir export --format graphml`. |
| 4 | `P2-neo4j` | Persistent backend for repos that don't fit in process memory. | Stub at `export/neo4j.py`. Translate `to_jsonable()` into Cypher MERGEs; provide a `Neo4jRepoGraph` that implements the same `RepoGraph` interface so passes don't notice. |

Acceptance for "P2 done": `cgir scan` runs on a 100k-LOC repo with the Neo4j backend in under five minutes, and Joern/CodeQL adapters produce specs that pass differential tests against the Tree-sitter pipeline.

## Beyond

These are *not* milestone-tagged — they're on the horizon but should not block the P2 backlog.

**Landed since this section was first written** (kept here as a record of what the "Beyond" bets became):

- **Grammar-agnostic core refactor — done.** The three tree-sitter-python couplings (analyses re-parsing source, hardcoded node-type strings in `cfg.py`/`effects.py`, Python-specific symbol resolution) are gone. A `LanguageAdapter` ABC (`languages/`) owns the language-specific bits (import resolution, builtin tables, what counts as an effect), and a plugin registry (`cgir.languages` entry points) lets third parties add languages out-of-tree.
- **TypeScript target — done.** Shipped as one adapter (`languages/typescript.py`), exactly as the refactor promised. **Go**, **Rust**, and **C** followed on the same seam.
- **Incremental indexing — done.** Content-hash parse cache (`languages/cache.py`) + `cgir watch`: unchanged files parse once ever; a single-file edit no longer reparses the world.
- **Regeneration validation — done (and generalized).** "Real trust" — compile + test the rewritten component before trusting it — is now the whole rewrite/verify stack: contract diff + shadow tests + differential oracle + whole-program gate, and `cgir verify-diff` as the standalone behavior-preservation gate.

Still genuinely on the horizon (no tag, no owner):

- **Trace edges as first-class graph data.** Move from a side-car `trace_map.json` to `TRACE_OF` edges on the `RepoGraph` directly, so trace queries are just graph queries.
- **Differential testing harness across backends.** Once the Joern/CodeQL bridges (P2) land, run them side-by-side with Tree-sitter on the same fixtures and diff the resulting `ComponentSpec`s — a precision metric and a backend-regression guard.

## How to pick what to work on

In order:

1. Anything with a written user-facing acceptance test that's currently red. There shouldn't be silently red tests on `main` — if you find one, finish it before opening a new front.
2. The three open P2 tags (`P2-neo4j`, `P2-joern-bridge`, `P2-codeql-bridge`) — the last remaining milestone stubs.
3. Deepening the rewrite north star (`vision-rewrite.md`): more rewrite pairs, more adapters, more oracle coverage. This is where the product's momentum is even though it's un-tagged.

When unsure, run `grep -rn "milestone:\|STUB:" src/` and pick a tag — every tag is a TDD entry point per [`development.md`](./development.md).
