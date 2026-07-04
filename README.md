# llm-semantic-compilers

**CodeGraph IR (CGIR)** — a semantic IR layer over repo graphs (Tree-sitter today, Joern/CodeQL planned). The goal is to turn a repository into small, traceable, language-agnostic `ComponentSpec` units that an LLM can rewrite, reassemble, and audit without holding the whole repo in context.

See [`Code-IR.md`](./Code-IR.md) for the full product specification and [`docs/`](./docs/) for working docs.

## Status

The Python-target pipeline runs end-to-end:

- Tree-sitter ingest → `Repository / File / Module / Function / Method / Class / Parameter / Import` nodes
- Symbol resolution + call-graph (`CONTAINS`, `IMPORTS`, `CALLS` edges)
- Effects classifier (`io`, `raise`, transitive `calls_effectful`) + purity scorer
- Component slicer with `kind` classification + JSON export
- `path:line → component_id` trace map
- Prompt-pack rendering (LLM call still stubbed)

The Python pipeline (ingest → symbols → call graph → CFG → reaching-defs → PDG → effects → purity → slice → export) runs end-to-end, with GraphML/HTML/Mermaid visualization. Joern/CodeQL adapters, Neo4j export, real LLM regeneration, and the FastAPI surface are stubbed with `milestone:` tags. Run `grep -rn "milestone:\|STUB:" src/` for the prioritized backlog, or see [`docs/roadmap.md`](./docs/roadmap.md).

## Quickstart

```bash
uv pip install -e ".[dev]"
pytest -q
cgir scan tests/fixtures/python_sample --out /tmp/cgir-out
cgir component pricing.add_tax --index /tmp/cgir-out
cgir trace pricing.py:1 --index /tmp/cgir-out
cgir viz --index /tmp/cgir-out                      # self-contained viz.html
cgir viz --index /tmp/cgir-out --format mermaid     # Markdown-embeddable flowchart
cgir export --format graphml --out /tmp/cgir-out    # Gephi / yEd
```

## Docs

- [`docs/architecture.md`](./docs/architecture.md) — layered pipeline, data model, extension seams
- [`docs/status.md`](./docs/status.md) — what runs today, test coverage, recent milestones
- [`docs/roadmap.md`](./docs/roadmap.md) — P1 / P2 / future sequencing
- [`docs/development.md`](./docs/development.md) — install, commands, red-green TDD workflow

## License

MIT — see [`LICENSE`](./LICENSE).
