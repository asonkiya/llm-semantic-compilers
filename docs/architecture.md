# Architecture

CGIR is a **layered pipeline** that turns a repository into a set of `ComponentSpec` JSON units. Each layer has a single responsibility and a stable contract, so backends can swap (Tree-sitter today; Joern/CodeQL later) without churning downstream code.

## Pipeline

```
            +---------------------------+
   repo --->|  Sources                  |   GraphSource ABC
            |  - TreeSitterSource (P0)  |   produces a RepoGraph with
            |  - JoernSource     (P2)   |   structural CONTAINS/IMPORTS
            |  - CodeQLSource    (P2)   |   edges + (path, line) on every node
            +-------------+-------------+
                          |
                          v   RepoGraph (skeleton)
            +---------------------------+
            |  Analyses                 |   functions in cgir.analyses.*
            |  - symbols       (P0)     |   resolve imports + module bindings
            |  - call_graph    (P0)     |   add CALLS edges
            |  - effects       (P0)     |   tag io / raise / calls_effectful
            |  - purity        (P0)     |   per-node score in [0, 1]
            |  - cfg / pdg / reaching   |   P1 stubs
            +-------------+-------------+
                          |
                          v   RepoGraph (enriched)
            +---------------------------+
            |  Slicing                  |   slice_components()
            |  Function/Method -> Spec  |
            +-------------+-------------+
                          |
                          v   list[ComponentSpec]
            +---------------------------+
            |  Export / Trace           |   json_export.write_index()
            |  - JSON         (P0)      |   + components/<id>.json
            |  - GraphML      (P2)      |   + trace_map.json
            |  - Neo4j        (P2)      |
            +-------------+-------------+
                          |
                          v
            +---------------------------+
            |  Surfaces                 |
            |  - CLI (typer)   (P0)     |   `cgir scan|component|trace|...`
            |  - HTTP (FastAPI)(P1 stub)|   501 placeholders
            |  - Regenerate   (P1 stub) |   prompt-pack + stub LLM call
            +---------------------------+
```

The CLI's `scan` command (`src/cgir/cli.py`) wires the layers together in the order above. New analyses should join that sequence at the right point; new backends should plug in at the `Sources` layer.

## Data model

The graph vocabulary is fixed by `Code-IR.md` §Data model and lives in `src/cgir/ir/`:

- **Nodes** — `NodeKind` enum (`src/cgir/ir/nodes.py`): `Repository`, `File`, `Module`, `Class`, `Function`, `Method`, `Parameter`, `Variable`, `Assignment`, `Expr`, `Statement`, `Branch`, `Loop`, `Return`, `Import`, `Effect`, `Test`.
- **Edges** — `EdgeKind` enum (`src/cgir/ir/edges.py`): `CONTAINS`, `IMPORTS`, `CALLS`, `READS`, `WRITES`, `MUTATES`, `RETURNS`, `THROWS`, `FLOWS_TO`, `CONTROLS`, `DEPENDS_ON`, `TRACE_OF`, `REGENERATED_AS`.

Do not introduce ad-hoc node or edge kinds. If a new relation seems necessary, propose it against the spec first.

`RepoGraph` (`src/cgir/ir/graph.py`) is a thin wrapper around `networkx.MultiDiGraph`. It exposes typed accessors (`nodes(kind=...)`, `out_edges(node_id, kind=...)`, `children(...)`) plus a `to_jsonable()` serializer. All backend-specific concerns stay below this interface.

### Today's edges

| Layer | Emits |
|---|---|
| `TreeSitterSource` | `Repository -[CONTAINS]-> File -[CONTAINS]-> Module -[CONTAINS]-> Function/Class`, `Class -[CONTAINS]-> Method`, `Function -[CONTAINS]-> Parameter`, `Module -[IMPORTS]-> Import` |
| `analyses.call_graph` | `Function -[CALLS]-> Function` (resolved through the symbol table) |
| Future P1 | `FLOWS_TO`, `CONTROLS`, `DEPENDS_ON` (via CFG/PDG/reaching defs) |
| Future P1 | `TRACE_OF` first-class on the graph (today the trace lives in `trace/trace_map.py` and the `trace` field on each spec) |

## ComponentSpec

The agent-facing contract. JSON schema lives in two places:

- `schemas/component_spec.schema.json` — canonical published copy.
- `src/cgir/ir/component_spec.py:COMPONENT_SPEC_SCHEMA` — runtime source of truth used by `Draft202012Validator`.

If you change the schema, change both and add a test.

Required fields: `id, kind, inputs, outputs, effects, calls, trace`. Optional: `language, signature, reads, writes, purity, algorithm`. `kind` is one of `pure_function | state_transformer | effect_adapter | orchestrator | unknown`.

`Slicer` classification (`src/cgir/slicing/slicer.py:_classify`):

| Tags / score | `kind` |
|---|---|
| Any direct tag in `DIRECT_EFFECT_TAGS` (`io`, `raise`, `net`, `fs`, `nondeterm`) | `effect_adapter` |
| Only `calls_effectful` (transitive only) | `orchestrator` |
| Empty effects + `purity == 1.0` | `pure_function` |
| Anything else | `unknown` |

`state_transformer` will land alongside `WRITES`/`MUTATES` edges (no milestone tag yet — falls under "extended effects taxonomy" in the roadmap).

## Effects taxonomy

Defined in `src/cgir/analyses/effects.py`:

- **Direct (today):** `io` (calls to `print` / `input` / `open`), `raise` (a `raise_statement` in the body).
- **Direct (planned):** `net`, `fs`, `nondeterm`. The taxonomy is centralized in `DIRECT_EFFECT_TAGS` so the slicer and purity scorer pick up new tags automatically.
- **Transitive:** `calls_effectful` — added to a function if any callee (transitively) has a direct effect. Computed by a fixed-point pass over `CALLS` edges.

Purity rubric (`src/cgir/analyses/purity.py:score`):

| Effects on the node | Score |
|---|---|
| Any direct tag | `0.0` |
| Only `calls_effectful` | `0.7` |
| Empty | `1.0` |

## Extension seams

### Add a new graph backend

Subclass `GraphSource` in `src/cgir/sources/base.py`. The contract is one method: `ingest(repo_path: Path) -> RepoGraph`. The Tree-sitter source (`tree_sitter_source.py`) is the reference implementation. Joern (`joern_source.py`) and CodeQL (`codeql_source.py`) are stubs against this same interface. New backends are picked up by the CLI via `CGIRConfig.source_backend`.

### Add a new analysis pass

Drop a module in `src/cgir/analyses/`. Public function takes a `RepoGraph` (plus whatever artifacts upstream passes produced, e.g. effects→purity) and returns a `dict[node_id, T]` or mutates the graph in place. Wire it into `src/cgir/cli.py:scan` in pipeline order.

### Add a new export format

Add a module in `src/cgir/export/`. Public function signature: `write(out_dir: Path, graph: RepoGraph, specs: list[ComponentSpec] | None = None) -> None`. Register it in the `cgir export --format` switch in `cli.py`.

### Add a new direct effect tag

1. Add the string to `DIRECT_EFFECT_TAGS` in `effects.py`.
2. Teach `_walk_body_for_effects` (or a sibling pass) how to detect it.
3. Add a RED test in `tests/unit/test_effects.py` first.

The slicer and purity scorer don't need touching — they read from the central set.

## Non-goals

Carried forward from `Code-IR.md`. CGIR will **not**:

- Replace a compiler. Tree-sitter / Joern / CodeQL stay as the parser/analyzer substrate.
- Guarantee semantic equivalence under dynamic features (`eval`, monkeypatch, reflection, async races). These are flagged and left as opaque.
- Emulate build systems or third-party source. Unresolved imports stay opaque `Import` nodes.
- Decompile cross-language perfectly. Regeneration is "preserve contract + trace", not "round-trip identity".

## Threat model

Local-first parsing is a deliberate choice — no network in the ingest or analysis passes. The optional LLM regenerate step (P1) is the only network surface, and it gates on a `ComponentSpec` rather than raw source so we control what crosses the boundary.
