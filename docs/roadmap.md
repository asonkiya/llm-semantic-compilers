# Roadmap

**Active plan: [`plan-0.3.md`](./plan-0.3.md)** — trust (confidence tiers), reach (go.mod resolution, pre-commit, agents), and the incremental-analysis design. `plan-0.2-0.4.md` is fully landed.

Forward-looking sequencing. The grouping mirrors `Code-IR.md` §Architecture: **P0** is "you can produce a `ComponentSpec`," **P1** is "you can trust it," **P2** is "you can scale it." Within each tier the order below reflects current dependencies, not strict chronology — feel free to interleave when dependencies allow.

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

**Sprint 3 (P1, in progress):**
- CFG extended with per-node `reads` (per-stmt sub-expression: RHS / condition / iterable / returned value), `controlled_by` (id of immediately enclosing Branch/Loop), and Assignment `mutates` (attribute/subscript LHS base names).
- PDG (`analyses/pdg.py`): emits `FLOWS_TO` (data dependence, gated by reaching-defs) and `DEPENDS_ON` (control dependence from `controlled_by`). Second pure-graph analysis.
- Slicer recognizes `state_transformer` when any Assignment child has non-empty `mutates`.
- Reaching-defs and PDG now wired into the CLI scan pipeline.

## P1 — Trust & explainability

The theme: make every `ComponentSpec` defensible. Today a function is "pure" if we couldn't *see* an effect; P1 makes that claim flow-sensitive, traceable to specific lines, and serveable over HTTP.

| # | Milestone tag | Why this comes first | Notes |
|---|---|---|---|
| ~~1~~ | ~~`P1-cfg`~~ | **Done (Sprint 1).** Unblocks 2 and 3. | `src/cgir/analyses/cfg.py`; 11 tests (later +4 for `writes` attr) in `tests/unit/test_cfg.py`. |
| ~~2~~ | ~~`P1-reaching-defs`~~ | **Done (Sprint 2).** Unblocks 3 and `WRITES`/`MUTATES` edges. | `src/cgir/analyses/reaching_defs.py`; 9 tests in `tests/unit/test_reaching_defs.py`. Pure-graph; wired into CLI in Sprint 3 alongside PDG. |
| ~~3~~ | ~~`P1-pdg`~~ | **Done (Sprint 3).** `FLOWS_TO` (data dep) + `DEPENDS_ON` (control dep). Lights up `state_transformer` in the slicer. | `src/cgir/analyses/pdg.py`; 10 tests in `tests/unit/test_pdg.py`. CFG extended with `reads`/`mutates`/`controlled_by` attrs to drive it. |
| 4 | Extended effects taxonomy (`net`, `fs`, `nondeterm`) | No milestone tag — drop tags into `effects.DIRECT_EFFECT_TAGS` and teach `_walk_body_for_effects` to detect them. | Start with the obvious imports (`requests`, `urllib`, `socket`, `os.path`, `pathlib.Path.write_text`, `time`, `random`, `datetime.now`). |
| 5 | Statement-granularity trace map | Today `trace_map.py` indexes function ranges. With the CFG in place we can resolve `path:line` to a specific Statement/Assignment/Branch/Loop node and the spec field that depends on it. | Refines `cgir trace` output. Now unblocked. |
| 6 | ~~`P1-regenerate`~~ **done (Sprint 9)** | Turn the prompt-pack into a real Anthropic SDK call. Add prompt caching from day one. | Landed: injectable generator seam; `anthropic_generator()` behind the `cgir[llm]` extra with cache_control on the system prompt. Compile/test round-trip verification before tagging `REGENERATED_AS` is still future work. |
| 7 | ~~`P1-api`~~ **done (Sprint 8)** | Replace 501 stubs in `api/server.py` with the real endpoints backed by the same passes the CLI uses. | Landed: `cgir/pipeline.py:scan_repo` is the single driver; CLI and FastAPI are thin surfaces over it. |

Acceptance for "P1 done": every `ComponentSpec` field has a real classifier behind it (no `PLACEHOLDER_SCORE` defaults firing in practice), `cgir regenerate` round-trips Python → TypeScript for the fixture, and the FastAPI surface mirrors the CLI.

## P2 — Scale

The theme: stop holding the graph in process memory, and accept secondary analyzers (Joern, CodeQL) for the cases where Tree-sitter alone is too shallow.

| # | Milestone tag | Why | Notes |
|---|---|---|---|
| 1 | `P2-joern-bridge` | CPG-style overlays give us real interprocedural data-flow without re-implementing it. | Implement as a `GraphSource` that shells out to Joern's CLI and normalizes its CPG into our `Node`/`Edge` vocabulary. |
| 2 | `P2-codeql-bridge` | Secondary analyzer + export bridge. Useful for differential testing against Joern. | Same pattern as Joern: shell out, normalize. |
| 3 | ~~`P2-graphml`~~ **done (Sprint 6)** | Cheap export for Gephi / yEd / Neo4j importers. | Landed in `export/graphml.py` — flattens attrs to GraphML-safe scalars. `cgir export --format graphml`. |
| 4 | `P2-neo4j` | Persistent backend for repos that don't fit in process memory. | Translate `to_jsonable()` into Cypher MERGEs; provide a `Neo4jRepoGraph` that implements the same `RepoGraph` interface so passes don't notice. |

Acceptance for "P2 done": `cgir scan` runs on a 100k-LOC repo with the Neo4j backend in under five minutes, and Joern/CodeQL adapters produce specs that pass differential tests against the Tree-sitter pipeline.

## Beyond

These are *not* milestone-tagged yet — they're on the horizon but should not block P1/P2 work.

- **Grammar-agnostic core refactor (architectural debt).** Today the `GraphSource` ABC is the only seam designed for non-tree-sitter parsers (PEG, ANTLR, hand-rolled, Joern, CodeQL). But three downstream modules currently bypass the abstraction and tie us specifically to `tree-sitter-python`:
  1. **Analyses re-parse source directly.** `analyses/call_graph.py`, `analyses/effects.py`, and `analyses/cfg.py` each import `tree_sitter_python` and walk function bodies themselves (look for the duplicated `_parser()` and `_locate_function()` helpers). A new `GraphSource` like Joern can't satisfy these passes — they'd still try to tree-sitter-parse the files.
  2. **Hardcoded tree-sitter node-type strings.** `cfg.py` switches on `"if_statement"` / `"for_statement"` / `"function_definition"`; `effects.py` looks for `"raise_statement"` and the `print`/`input`/`open` builtins. Tree-sitter-python's grammar is leaking into language-agnostic passes.
  3. **Symbol resolution is Python-specific.** `analyses/symbols.py` assumes `from a.b import c` semantics and dotted module names derived from file paths.

  Two refactor moves close this debt: (a) push fine-grained AST extraction down into `GraphSource` so it emits `Call` / `Raise` / `Statement` / `Branch` nodes at ingest time (consistent with the unused `Expr`/`Statement` items in the spec's vocabulary), and (b) introduce a `LanguageAdapter` ABC for genuinely language-specific bits (import resolution, builtin tables, what counts as an effect).

  **Cheap path:** opportunistic refactor — when the next milestone touches one of those modules (`P1-reaching-defs` will touch the CFG output; extended-effects will touch `effects.py`), refactor that module to read from `RepoGraph` instead of re-parsing. After 2–3 such cycles the tree-sitter coupling is confined to `TreeSitterSource` only. **Expensive path:** dedicated sprint if a non-tree-sitter backend lands before opportunistic cleanup finishes.

- **TypeScript target.** Mirror the Python ingester using `tree-sitter-typescript`. **Note:** until the grammar-agnostic refactor above lands, this is *not* "just write a new `GraphSource`" — every Python-specific hardcoded node type in `cfg.py` / `effects.py` would need a TS twin. Doing the refactor first turns TS support into roughly one `GraphSource` + one `LanguageAdapter`.
- **Regeneration validation.** Real "trust": compile + test the LLM-regenerated component and only emit a `REGENERATED_AS` edge when it passes. Probably ships with `P1-regenerate` v2.
- **Trace edges as first-class graph data.** Move from a side-car `trace_map.json` to `TRACE_OF` edges on the `RepoGraph` directly, so trace queries are just graph queries.
- **Incremental indexing.** Tree-sitter is incremental; the ingester is not yet. Once repos get big, add a content-hash cache so a single-file edit doesn't reparse the world.
- **Differential testing harness.** Run Joern, CodeQL, and Tree-sitter side-by-side on the same fixtures and diff the resulting `ComponentSpec`s. Useful both for precision metrics and for spotting backend regressions.

## How to pick what to work on

In order:

1. Anything that unblocks **other** P1 milestones (CFG → reaching defs → PDG is a strict chain).
2. Anything with a written user-facing acceptance test that's currently red. There shouldn't be silently red tests on `main` — if you find one, finish it before opening a new front.
3. P1 items in priority order from the table above.
4. P2 only after P1 is fully green and the CLI no longer surfaces placeholder values to users.

When unsure, run `grep -rn "milestone:\|STUB:" src/` and pick a tag — every tag is a TDD entry point per [`development.md`](./development.md).
