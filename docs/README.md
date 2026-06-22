# CGIR docs

These are the working docs for **CodeGraph IR** (CGIR). The authoritative product spec lives at [`../Code-IR.md`](../Code-IR.md); this folder is the day-to-day reference for engineers (and Claude) actually building it.

| Doc | What it covers |
|---|---|
| [`architecture.md`](./architecture.md) | Layered pipeline, module map, data model (`Node`/`Edge`/`RepoGraph`), `ComponentSpec` contract, extension seams |
| [`status.md`](./status.md) | What's implemented today, what's stubbed, test coverage, recent milestone completions |
| [`roadmap.md`](./roadmap.md) | P1 / P2 / future milestones with ordering rationale |
| [`development.md`](./development.md) | Install, common commands, red-green TDD workflow, milestone-tag conventions |

If you're starting cold:

1. Skim `architecture.md` to get the pipeline shape.
2. Read `status.md` for what runs today.
3. Read `development.md` before writing code — the TDD workflow and milestone-tag convention are how we move forward.
4. `roadmap.md` answers "what should I work on next".

The single source of truth for working *conventions Claude must follow* is [`../CLAUDE.md`](../CLAUDE.md). If `CLAUDE.md` and a doc here disagree, fix one of them — don't leave the drift.
