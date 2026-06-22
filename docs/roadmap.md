# Roadmap

Forward-looking sequencing. The grouping mirrors `Code-IR.md` §Architecture: **P0** is "you can produce a `ComponentSpec`," **P1** is "you can trust it," **P2** is "you can scale it." Within each tier the order below reflects current dependencies, not strict chronology — feel free to interleave when dependencies allow.

## Done (P0)

- Tree-sitter ingest → structural skeleton (`Repository / File / Module / Function / Method / Class / Parameter / Import`).
- Symbol resolution + cross-file imports.
- Call graph (`CALLS` edges).
- Effects classifier (`io`, `raise`, transitive `calls_effectful`).
- Purity scorer (1.0 / 0.7 / 0.0).
- Slicer + ComponentSpec JSON export + trace map + CLI.

## P1 — Trust & explainability

The theme: make every `ComponentSpec` defensible. Today a function is "pure" if we couldn't *see* an effect; P1 makes that claim flow-sensitive, traceable to specific lines, and serveable over HTTP.

| # | Milestone tag | Why this comes first | Notes |
|---|---|---|---|
| 1 | `P1-cfg` | Per-function control-flow graph unblocks reaching-defs, PDG, and richer effect detection (e.g. `raise` inside dead branches shouldn't count). | Build on tree-sitter; emit `CONTROLS` edges on the existing graph. |
| 2 | `P1-reaching-defs` | Needed for any meaningful data-flow claim. Also a prerequisite for `WRITES`/`MUTATES` edges. | Worklist algorithm over the CFG; intra-procedural first. |
| 3 | `P1-pdg` | Combines CFG + reaching defs into the spec's `FLOWS_TO` / `CONTROLS` / `DEPENDS_ON` edges. Refines `kind` classification. | Once landed, the slicer can claim `state_transformer` for functions that mutate locals/params. |
| 4 | Extended effects taxonomy (`net`, `fs`, `nondeterm`) | No milestone tag — drop tags into `effects.DIRECT_EFFECT_TAGS` and teach `_walk_body_for_effects` to detect them. | Start with the obvious imports (`requests`, `urllib`, `socket`, `os.path`, `pathlib.Path.write_text`, `time`, `random`, `datetime.now`). |
| 5 | Statement-granularity trace map | Today `trace_map.py` indexes function ranges. With a CFG we can resolve `path:line` to a specific statement and the spec field that depends on it. | Refines `cgir trace` output. |
| 6 | `P1-regenerate` | Turn the prompt-pack into a real Anthropic SDK call. Add prompt caching from day one. | Skill `claude-api` lists the patterns to follow. |
| 7 | `P1-api` | Replace 501 stubs in `api/server.py` with the real endpoints backed by the same passes the CLI uses. | Keep the CLI as the single pipeline driver — the API should call the same functions. |

Acceptance for "P1 done": every `ComponentSpec` field has a real classifier behind it (no `PLACEHOLDER_SCORE` defaults firing in practice), `cgir regenerate` round-trips Python → TypeScript for the fixture, and the FastAPI surface mirrors the CLI.

## P2 — Scale

The theme: stop holding the graph in process memory, and accept secondary analyzers (Joern, CodeQL) for the cases where Tree-sitter alone is too shallow.

| # | Milestone tag | Why | Notes |
|---|---|---|---|
| 1 | `P2-joern-bridge` | CPG-style overlays give us real interprocedural data-flow without re-implementing it. | Implement as a `GraphSource` that shells out to Joern's CLI and normalizes its CPG into our `Node`/`Edge` vocabulary. |
| 2 | `P2-codeql-bridge` | Secondary analyzer + export bridge. Useful for differential testing against Joern. | Same pattern as Joern: shell out, normalize. |
| 3 | `P2-graphml` | Cheap export for Gephi / yEd / Neo4j importers. | Use `networkx.write_graphml` on `RepoGraph._g`. |
| 4 | `P2-neo4j` | Persistent backend for repos that don't fit in process memory. | Translate `to_jsonable()` into Cypher MERGEs; provide a `Neo4jRepoGraph` that implements the same `RepoGraph` interface so passes don't notice. |

Acceptance for "P2 done": `cgir scan` runs on a 100k-LOC repo with the Neo4j backend in under five minutes, and Joern/CodeQL adapters produce specs that pass differential tests against the Tree-sitter pipeline.

## Beyond

These are *not* milestone-tagged yet — they're on the horizon but should not block P1/P2 work.

- **TypeScript target.** Mirror the Python ingester using `tree-sitter-typescript`. Most analyses are language-agnostic once they're operating on the normalized graph; the new work is symbol resolution and ts-specific call-site disambiguation.
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
