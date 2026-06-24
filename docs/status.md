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
| Reaching definitions (worklist over `CONTROLS`) | done | `src/cgir/analyses/reaching_defs.py` |
| PDG: `FLOWS_TO` (data dep) + `DEPENDS_ON` (control dep) | done | `src/cgir/analyses/pdg.py` |
| `state_transformer` classification (attribute/subscript assignment) | done | `src/cgir/slicing/slicer.py:_has_mutations` |
| Shared tree-sitter helper (first opportunistic step on grammar-agnostic refactor) | done | `src/cgir/analyses/_python_ast.py` |
| Extended effects taxonomy (`net`, `fs`, `nondeterm`) | planned | extends `effects.DIRECT_EFFECT_TAGS` |
| PDG overlay | stub | `src/cgir/analyses/pdg.py` (`P1-pdg`) |
| LLM-driven regeneration | stub | `src/cgir/regenerate/regenerator.py` (`P1-regenerate`) |
| HTTP API (FastAPI) | stub | `src/cgir/api/server.py` (`P1-api`, 501s) |
| Joern adapter | stub | `src/cgir/sources/joern_source.py` (`P2-joern-bridge`) |
| CodeQL adapter | stub | `src/cgir/sources/codeql_source.py` (`P2-codeql-bridge`) |
| GraphML export | stub | `src/cgir/export/graphml.py` (`P2-graphml`) |
| Neo4j export | stub | `src/cgir/export/neo4j.py` (`P2-neo4j`) |
| TypeScript target | deferred | no module yet |

## Test coverage

`pytest -q` runs 90 tests, all green:

| File | Covers |
|---|---|
| `tests/unit/test_ir_graph.py` | RepoGraph add/query, JSON serialization |
| `tests/unit/test_component_spec.py` | Schema round-trip + invalid-kind rejection |
| `tests/unit/test_tree_sitter_source.py` | File / function / parameter ingest counts; default ignore-dirs (venv, node_modules, build, dist, __pycache__, site-packages); custom ignore extends default; dot-prefix dirs; decorated functions (@property, @staticmethod, @classmethod, multi-decorator stack, decorated class) |
| `tests/unit/test_symbols.py` | Local function/class bindings; absolute `from a.b import c`; relative imports (`.x`, `..x`); relative imports drive `CALLS`; unresolved external imports stay opaque; `IMPORTS` edge target attribute |
| `tests/unit/test_call_graph.py` | Cross-file `CALLS` resolution |
| `tests/unit/test_effects.py` | Pure / io / raise / transitive / per-function coverage |
| `tests/unit/test_purity.py` | 1.0 / 0.7 / 0.0 tiers, pure caller stays pure |
| `tests/unit/test_cfg.py` | CFG topology (chain, if/else, if/elif/else, for, while, return-as-sink, nested); Assignment `writes`/`mutates` for simple/tuple/subscript/attribute LHS; per-node `reads` (RHS, condition, iterable, returned value; excludes attribute names and callee names); `controlled_by` threading through nested branches and loops |
| `tests/unit/test_reaching_defs.py` | Pure-graph signature, linear def→use, kill on reassignment, branch-merge union, parameter as initial def, loop back-edge propagation, var-isolation, empty-function shape, full-coverage shape |
| `tests/unit/test_pdg.py` | Pure-graph signature; `FLOWS_TO` for linear/reassignment/parameter/branch-merge; no flow for unread defs; var-filtered flow; `DEPENDS_ON` for if-body and loop-body; no control-dep for top-level stmts |
| `tests/unit/test_slicer.py` | `pure_function` regression; method mutating `self.x` classifies as `state_transformer` |
| `tests/unit/test_slicer.py` | `pure_function` classification + `purity == 1.0` |
| `tests/unit/test_trace_map.py` | path:line lookup |
| `tests/integration/test_cli_scan.py` | Full CLI pipeline writes correct outputs |

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

## Known precision gaps

Real codebases will hit these — flag rather than guess:

- **Method-call mutations not detected.** `xs.append(x)`, `self.config.update(...)`, etc. are calls (no LHS), so they don't trigger `mutates`. Functions that mutate via method calls stay classified as `pure_function`.
- **Augmented assignments aren't extracted.** `x += 1` and `self.counter += 1` are tree-sitter `augmented_assignment` nodes; the CFG only handles plain `assignment`. So they're not in `writes`/`mutates`/`reads`. Affects reaching-defs and PDG precision inside any function using `+=` / `-=` / `*=` / etc.
- **Body of `with` / `try` / `match` is opaque.** Statements inside these constructs become a single CFG `Statement` node and their inner statements never enter the graph. Lifted in Sprint 5.
- **Effects taxonomy is minimal.** Only `io` (print/input/open) and `raise` are detected directly; `net`, `fs`, `nondeterm` are documented but not yet wired into `_walk_body_for_effects`.

## Outstanding tags

`grep -rn "milestone:\|STUB:" src/` is the canonical backlog. As of this commit it lists:

- `P1-api`, `P1-regenerate`
- `P2-graphml`, `P2-neo4j`, `P2-joern-bridge`, `P2-codeql-bridge`

See [`roadmap.md`](./roadmap.md) for sequencing.
