# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Where the docs live

- [`Code-IR.md`](./Code-IR.md) — authoritative product specification.
- [`docs/`](./docs/) — working docs for engineers and Claude. Start at [`docs/README.md`](./docs/README.md). Key files:
  - [`docs/architecture.md`](./docs/architecture.md) — layered pipeline, data model, extension seams.
  - [`docs/status.md`](./docs/status.md) — what runs today vs. what's stubbed.
  - [`docs/roadmap.md`](./docs/roadmap.md) — milestone sequencing.
  - [`docs/development.md`](./docs/development.md) — install, commands, **red-green TDD workflow**, milestone-tag convention.

When `CLAUDE.md` and a doc disagree, the doc is canonical for content; this file is canonical for "what Claude must remember to do."

## Common commands

```bash
uv pip install -e ".[dev,api]"
pytest -q
ruff check . && ruff format --check . && mypy src
cgir scan tests/fixtures/python_sample --out /tmp/cgir-out
```

Full command catalogue and CI workflow are in [`docs/development.md`](./docs/development.md).

## Working conventions (must-follow)

- **Red-green TDD for milestones.** Every `milestone:` or `# STUB:` tag is a TDD entry point. Cycle: write failing tests pinning the public contract → implement until green → refactor. Detail in [`docs/development.md`](./docs/development.md). Don't skip the red phase — even in auto mode.
- **Milestone-tag hygiene.** Backlog is `grep -rn "milestone:\|STUB:" src/`. Completing a milestone means the tag *literally disappears*. Don't leave both real and stub paths in source.
- **Pipeline order is fixed.** New analyses wire into `src/cgir/cli.py:scan` in the order `ingest → symbols → call_graph → effects → purity → slice → export`. New graph backends subclass `GraphSource` in `src/cgir/sources/base.py`.
- **Vocabulary is fixed by the spec.** `NodeKind` and `EdgeKind` enums in `src/cgir/ir/` come straight from `Code-IR.md` §Data model. Don't add ad-hoc kinds without updating the spec first.
- **`ComponentSpec` is the agent-facing contract.** Schema lives in two places: `schemas/component_spec.schema.json` (published) and `src/cgir/ir/component_spec.py:COMPONENT_SPEC_SCHEMA` (runtime source of truth). Change both, add a schema test.
- **Local-first parsing.** No network in the ingest or analysis layers. Only the optional regeneration step touches an LLM, and it gates on `ComponentSpec` rather than raw source.

## Out of scope (per spec)

Don't propose work in these directions without explicit user buy-in: full compiler replacement, exact semantic equivalence for all dynamic/runtime features, build-system emulation, or perfect cross-language decompilation. Dynamic dispatch, `eval`, monkeypatching, and reflection are acknowledged precision limits — flag them rather than try to solve them perfectly.
