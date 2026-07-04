# Status

Snapshot of where CGIR is today. Source of truth for "what runs" is `pytest -q`; source of truth for "what's stubbed" is `grep -rn "milestone:\|STUB:" src/`.

## What works end-to-end

The Python ingest → ComponentSpec pipeline runs:

```
cgir scan tests/fixtures/python_sample --out /tmp/cgir-out
```

…produces a `repo_graph.json`, `components/*.json` (validated against the schema), `components_index.json`, and `trace_map.json`. On the in-tree fixture both `pricing.add_tax` and `orchestrator.quote` classify as `pure_function` with `purity: 1.0` and the cross-file `CALLS` edge resolves correctly.

## Feature matrix

| Area | Status | Where |
|---|---|---|
| Project tooling (pyproject, ruff, mypy, CI matrix) | done | `pyproject.toml`, `.github/workflows/ci.yml` |
| ComponentSpec schema | done | `src/cgir/ir/component_spec.py`, `schemas/component_spec.schema.json` |
| IR core (Node/Edge/RepoGraph) | done | `src/cgir/ir/` |
| Runtime config | done | `src/cgir/config.py` |
| GraphSource ABC | done | `src/cgir/sources/base.py` |
| Tree-sitter Python ingester (with default ignore-dirs and `--exclude`) | done | `src/cgir/sources/tree_sitter_source.py` (`DEFAULT_IGNORE_DIRS`) |
| Decorated function / class definitions surfaced | done | `tree_sitter_source._dispatch_top_level`, `_dispatch_in_class` |
| Symbol resolution + absolute and relative import resolution | done | `src/cgir/analyses/symbols.py`, `tree_sitter_source._resolve_from_module` |
| CLI per-kind histogram + `--exclude` flag | done | `src/cgir/cli.py:_print_kind_histogram` |
| Call graph (CALLS edges) | done | `src/cgir/analyses/call_graph.py` |
| Effects classifier (`io`, `raise`, transitive) | done | `src/cgir/analyses/effects.py` |
| Purity scorer (1.0 / 0.7 / 0.0) | done | `src/cgir/analyses/purity.py` |
| Component slicer + kind classification | done | `src/cgir/slicing/slicer.py` |
| JSON export | done | `src/cgir/export/json_export.py` |
| Trace map (function granularity) | done | `src/cgir/trace/trace_map.py` |
| Prompt-pack rendering | done | `src/cgir/regenerate/prompt_pack.py` |
| CLI (`scan`, `export`, `component`, `trace`, `regenerate`) | done | `src/cgir/cli.py` |
| Intra-procedural CFG (`Statement`/`Assignment`/`Branch`/`Loop`/`Return` + `CONTROLS` edges) | done | `src/cgir/analyses/cfg.py` |
| Assignment `writes` / `mutates` attrs; per-node `reads` / `controlled_by` attrs | done | `src/cgir/analyses/cfg.py` (`_extract_lhs_targets`, `_extract_reads`) |
| `with` / `try` / `match` body traversal (headers as defs, except/case as Branch) | done | `src/cgir/analyses/cfg.py` (`_build_with`, `_build_try`, `_build_match`) |
| Augmented assignment (`x += 1`, `self.total += n`) in writes/mutates/reads | done | `src/cgir/analyses/cfg.py` (`_ASSIGNMENT_TYPES`) |
| Bare mutator method calls (`xs.append(x)`) recorded as `mutates` | done | `src/cgir/analyses/cfg.py` (`_extract_call_mutations`, `_MUTATOR_METHODS`) |
| `for`-target / `with`-alias / `except`-alias as reaching definitions | done | `cfg.py` + generalized defs in `reaching_defs.py` / `pdg.py` |
| Caller-observable mutation gate (local-object mutation stays pure) | done | `src/cgir/slicing/slicer.py:_has_mutations` |
| Reaching definitions (worklist over `CONTROLS`) | done | `src/cgir/analyses/reaching_defs.py` |
| PDG: `FLOWS_TO` (data dep) + `DEPENDS_ON` (control dep) | done | `src/cgir/analyses/pdg.py` |
| `state_transformer` classification (attribute/subscript assignment) | done | `src/cgir/slicing/slicer.py:_has_mutations` |
| Shared tree-sitter helper (first opportunistic step on grammar-agnostic refactor) | done | `src/cgir/analyses/_python_ast.py` |
| Extended effects taxonomy (`net`, `fs`, `nondeterm`, lexical matching) | done | `src/cgir/analyses/effects.py` (`_classify_dotted_call`) |
| Regeneration with injectable generator seam + Anthropic backend (`--live`) | done | `src/cgir/regenerate/regenerator.py` (`anthropic_generator`, prompt caching on) |
| GraphML export (`cgir export --format graphml`) | done | `src/cgir/export/graphml.py` |
| Interactive HTML viz (`cgir viz`) — self-contained, no network | done | `src/cgir/export/html_viz.py` |
| Mermaid call-graph (`cgir viz --format mermaid`) | done | `src/cgir/export/mermaid.py` |
| Structure report (`cgir stats`, `--json`) — kinds, purity, effects, hotspots | done | `src/cgir/report/stats.py` |
| Shared pipeline driver (CLI + API call the same function) | done | `src/cgir/pipeline.py:scan_repo` |
| HTTP API (`/scan`, `/components`, `/trace`, `/regenerate`, `/stats`) | done | `src/cgir/api/server.py` |
| `RepoGraph.from_jsonable` (viz/export run off an existing index) | done | `src/cgir/ir/graph.py` |
| Joern adapter | stub | `src/cgir/sources/joern_source.py` (`P2-joern-bridge`) |
| CodeQL adapter | stub | `src/cgir/sources/codeql_source.py` (`P2-codeql-bridge`) |
| Neo4j export | stub | `src/cgir/export/neo4j.py` (`P2-neo4j`) |
| TypeScript target | deferred | no module yet |

## Test coverage

`pytest -q` runs 173 tests, all green:

| File | Covers |
|---|---|
| `tests/unit/test_ir_graph.py` | RepoGraph add/query, JSON serialization |
| `tests/unit/test_component_spec.py` | Schema round-trip + invalid-kind rejection |
| `tests/unit/test_tree_sitter_source.py` | File / function / parameter ingest counts; default ignore-dirs (venv, node_modules, build, dist, __pycache__, site-packages); custom ignore extends default; dot-prefix dirs; decorated functions (@property, @staticmethod, @classmethod, multi-decorator stack, decorated class) |
| `tests/unit/test_symbols.py` | Local function/class bindings; absolute `from a.b import c`; relative imports (`.x`, `..x`); relative imports drive `CALLS`; unresolved external imports stay opaque; `IMPORTS` edge target attribute |
| `tests/unit/test_call_graph.py` | Cross-file `CALLS` resolution |
| `tests/unit/test_effects.py` | Pure / io / raise / transitive / per-function coverage; net (requests, urllib), fs (os.remove, shutil, .write_text), nondeterm (random, time.time, datetime.now, uuid4); arbitrary method calls stay untagged |
| `tests/unit/test_purity.py` | 1.0 / 0.7 / 0.0 tiers, pure caller stays pure |
| `tests/unit/test_cfg.py` | CFG topology (chain, if/else, if/elif/else, for, while, return-as-sink, nested); Assignment `writes`/`mutates` for simple/tuple/subscript/attribute LHS; per-node `reads` (RHS, condition, iterable, returned value; excludes attribute names and callee names); `controlled_by` threading through nested branches and loops; `with` bodies + header alias writes/context reads; `try`/`except`/`else`/`finally` bodies, except-as-Branch, except alias writes; `match` case Branch chains, case-body control deps, subject reads; augmented assignment writes/mutates/reads; mutator-call `mutates` (`xs.append`, chained `self.config.update`, non-mutator negative); for-target writes (simple + tuple) |
| `tests/unit/test_reaching_defs.py` | Pure-graph signature, linear def→use, kill on reassignment, branch-merge union, parameter as initial def, loop back-edge propagation, var-isolation, empty-function shape, full-coverage shape; with-alias and for-target as defs |
| `tests/unit/test_pdg.py` | Pure-graph signature; `FLOWS_TO` for linear/reassignment/parameter/branch-merge; no flow for unread defs; var-filtered flow; `DEPENDS_ON` for if-body and loop-body; no control-dep for top-level stmts; for-target and with-alias `FLOWS_TO` body uses |
| `tests/unit/test_slicer.py` | `pure_function` regression + `purity == 1.0`; `self.x` mutation, `xs.append(x)`, and `self.total += n` classify as `state_transformer`; mutating a *local* list/dict stays `pure_function`; mutating a module-level global counts |
| `tests/unit/test_trace_map.py` | path:line lookup |
| `tests/unit/test_graphml.py` | File written, loads back via `nx.read_graphml`, scalar attrs (lists JSON-encoded, None dropped), edge kinds preserved |
| `tests/unit/test_mermaid.py` | Flowchart header, dotted labels, sanitized edge ids, subgraph per file, kind classDef/class styling, external calls skipped |
| `tests/unit/test_html_viz.py` | `viz.html` written, data embedded, **no external resources**, JSON island parseable |
| `tests/unit/test_stats.py` | Totals/files, kind counts, purity buckets + mean, effect counts, most-called ranking, fan-out ranking, external-call counting, empty-input shape |
| `tests/integration/test_cli_scan.py` | Full CLI pipeline writes correct outputs; `cgir viz` (html + mermaid), `cgir export --format graphml`, and `cgir stats` (text + `--json`) run off an existing index |
| `tests/integration/test_api.py` | `POST /scan` → list/get components, trace hit + 404 miss, regenerate prompt-pack, stats; 409 on unscanned index; 404 on unknown component |
| `tests/unit/test_regenerator.py` | Injected generator drives live result; generator receives the prompt-pack; dry run without generator (no STUB markers); missing `anthropic` raises install hint |

The `test_symbols.py` row is intentional debt — symbol resolution is exercised transitively by the call-graph tests but doesn't have a direct red-green pair yet. Pick it up before any change to `analyses/symbols.py`.

## Recent milestones

| When | Milestone | How |
|---|---|---|
| Sprint 0 | Initial scaffold | Manual implementation per `goofy-zooming-clock.md` plan |
| Sprint 0 | P0-effects | Red-green TDD — `tests/unit/test_effects.py` first, then `analyses/effects.py` |
| Sprint 0 | P0-purity | Red-green TDD — `tests/unit/test_purity.py` first, then `analyses/purity.py` |
| Sprint 0 | Slicer `kind` classification | Test update in `test_slicer.py` first, then `_classify` rewrite |
| Sprint 1 | P1-cfg | Red-green TDD — `tests/unit/test_cfg.py` (11 tests) first, then `analyses/cfg.py`. Wired between `call_graph` and `effects` in the CLI pipeline. |
| Sprint 2 | P1-reaching-defs | Red-green TDD — extended `test_cfg.py` with `writes`-attr tests, added `test_reaching_defs.py` (9 tests). Implemented worklist may-analysis in `analyses/reaching_defs.py` as the first pure-graph (no `repo_path`) analysis. |
| Sprint 2 | Shared tree-sitter helper | Refactor step after Sprint 2 green: extracted duplicated `_parser` / `_locate_function` from `call_graph`, `effects`, `cfg` into `analyses/_python_ast.py`. First opportunistic step on the grammar-agnostic core refactor (see `roadmap.md` "Beyond"). |
| Sprint 3 | P1-pdg | Red-green TDD — extended CFG with `reads`/`mutates`/`controlled_by` attrs (16 new test_cfg.py tests); added `test_pdg.py` (10 tests) for `FLOWS_TO` (data dep) and `DEPENDS_ON` (control dep). Second pure-graph analysis. Wired reaching-defs + PDG into the CLI scan pipeline. |
| Sprint 3 | `state_transformer` classification | Slicer reads `Assignment.attrs["mutates"]` to detect functions that mutate via attribute or subscript LHS. `tests/unit/test_slicer.py` pins a method `set_x(self, v): self.x = v` as `state_transformer`. |
| Sprint 4 | Real-world usability fixes | Ingester now skips `DEFAULT_IGNORE_DIRS` ({venv, node_modules, build, dist, __pycache__, site-packages, .tox, .pytest_cache, .mypy_cache, .ruff_cache, target, out, env}) and accepts a `--exclude` flag for custom names. Decorated functions and classes (`@property`/`@staticmethod`/`@classmethod`/multi-decorator stacks) are now surfaced. Relative imports (`from .x import y`, `from ..a.b import c`) resolve to absolute targets and feed the `CALLS` resolver. CLI scan prints a per-kind histogram after writing the index. Smoke-tested on the CGIR codebase itself: `cgir scan .` produces 219 components with sane distribution and runs in ~1s. |
| Sprint 9 | `P1-regenerate` | Red-green TDD — 4 new tests. `regenerate(spec, lang, generator=None)`: the LLM call is an injectable `Callable[[str], str]`, so everything is testable offline. `anthropic_generator()` (lazy import, `cgir[llm]` extra, `CGIR_MODEL` override, prompt caching on the system prompt from day one per roadmap) backs `cgir regenerate --live`; without `--live` it's an explicit dry run that prints the prompt-pack. API `/regenerate` stays dry-run. |
| Sprint 8 | `P1-api` | Red-green TDD — 8 new tests. Extracted the scan pipeline into `cgir/pipeline.py:scan_repo` (per roadmap: one driver, CLI and API as thin surfaces). FastAPI routes: `POST /scan`, `GET /components`, `GET /components/{id}`, `GET /trace`, `POST /regenerate`, `GET /stats`. Missing-index reads answer 409 (vs 404 for unknown components). `CLAUDE.md` pipeline pointer updated to `pipeline.py`. |
| Sprint 7 | `cgir stats` structure report | Red-green TDD — 10 new tests. `report/stats.py:compute_stats` is a pure function over specs (JSON-able result); `render_text` for the terminal. Reports totals, per-kind counts, purity buckets (pure/tainted/impure + mean), effect tag counts, most-called components, top fan-out, and external-call hotspots. `cgir stats --index <dir> [--json]`. |
| Sprint 6 | Visualization (`P2-graphml` + `cgir viz`) | Red-green TDD — 18 new tests. `RepoGraph.from_jsonable` lets `viz`/`export` run off an existing `.cgir` index without rescanning. GraphML export flattens attrs to scalars for Gephi/yEd. `cgir viz` writes a fully self-contained `viz.html` (embedded JSON + vanilla-JS canvas force layout: drag, zoom, pan, tooltip, detail panel, search, kind legend — zero network requests). `cgir viz --format mermaid` prints a Markdown-embeddable flowchart, subgraph per file, styled by component kind. |
| Sprint 5 | Precision fixes (closes all four Sprint-4 gaps) | Red-green TDD — 43 new tests. `with`/`try`/`match` bodies now traversed: `with` headers define their `as` aliases and keep the outer controller; `except` clauses become `Branch` nodes (handler bodies control-dependent, `as exc` alias is a def); `match` cases mirror if/elif Branch chains. Augmented assignments feed writes/mutates/reads. Bare mutator calls (`xs.append(x)`, `self.config.update(d)`) record `mutates` via a known-mutator-method table. `for` targets are defs. `reaching_defs`/`pdg` generalized: any CFG node with non-empty `writes` is a definition. Effects taxonomy extended with lexical `net`/`fs`/`nondeterm` matching (`_classify_dotted_call`). Slicer now gates mutation on caller observability: mutating a locally-created object stays pure — self-scan `state_transformer` count dropped from 32 (mostly false) to 5 (all true). |

## Known precision gaps

Real codebases will hit these — flag rather than guess:

- **Effect matching is lexical.** `net`/`fs`/`nondeterm` match the dotted callee text against prefix/suffix tables. `import requests as r; r.get(url)` escapes; `self.now()` false-positives on the `.now` suffix. Symbol-resolved effect matching is future work.
- **Mutator-method detection is a fixed name table.** `_MUTATOR_METHODS` covers the common list/dict/set/deque/queue/file mutators. Unknown mutator names and calls whose result is consumed (`x = xs.pop()`) are missed.
- **`case` patterns don't bind.** `case Point(x=a):` binds `a`, but pattern captures aren't extracted as writes — only the subject read and guard reads are recorded.
- **`break` / `continue` jump targets** aren't modelled; loop `else` clauses and exception flow *within* a try body (a raise mid-block skipping the rest) are approximated.
- **Local-mutation gate is name-based.** A local name rebound to a parameter (`alias = xs; alias.append(x)`) is treated as local and stays pure — no alias analysis.

## Outstanding tags

`grep -rn "milestone:\|STUB:" src/` is the canonical backlog. As of this commit it lists:

- `P2-neo4j`, `P2-joern-bridge`, `P2-codeql-bridge`

P0 and P1 are complete.

See [`roadmap.md`](./roadmap.md) for sequencing.
