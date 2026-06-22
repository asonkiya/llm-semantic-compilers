# Development

How to install, run, and contribute changes. Read this before writing code.

## Install

```bash
uv venv --python 3.11 .venv
source .venv/bin/activate
uv pip install -e ".[dev,api]"
```

`[api]` pulls in FastAPI/uvicorn (only needed if you're touching `src/cgir/api/`). `[dev]` is required for tests, lint, and type-checking.

## Common commands

```bash
pytest -q                                    # full suite (target: green on every commit)
pytest tests/unit/test_effects.py            # one file
pytest -k transitive                         # by name pattern
pytest -q --cov=src/cgir                     # with coverage

ruff check .                                 # lint
ruff format .                                # apply formatter
ruff format --check .                        # CI-equivalent formatter check
mypy src                                     # strict type-check

cgir scan tests/fixtures/python_sample --out /tmp/cgir-out
cgir component pricing.add_tax --index /tmp/cgir-out
cgir trace pricing.py:1 --index /tmp/cgir-out
cgir regenerate pricing.add_tax --lang typescript --index /tmp/cgir-out
```

CI (`.github/workflows/ci.yml`) runs ruff check + ruff format check + mypy + pytest on a 3.11/3.12 matrix. If a change passes locally on 3.11 it should pass CI; if it doesn't, treat that as a real regression.

## Red-green TDD for milestones

We drive deferred work in `Code-IR.md` via test-first development. Every milestone tag (see "Milestone-tag convention" below) is a TDD entry point.

### The cycle

1. **Red.**
   - Write the failing tests in `tests/unit/test_<pass>.py` that pin down the public contract: function signature, return shape, edge cases (including transitive behavior where relevant).
   - Update any existing tests whose assertions depended on stub values.
   - Run `pytest -q` and confirm the failures are about the *missing behavior*, not import errors or signature drift. If the failures look like noise, fix the test setup first.

2. **Green.**
   - Implement the smallest change that turns every red test green.
   - Remove the corresponding `milestone:` or `# STUB:` tag from source.
   - If the pass needs to plumb through new arguments (e.g. `classify(graph, repo_path)`), update *all* callers in the same change — usually `cli.py` plus any other passes that compose with it.

3. **Refactor.**
   - Only after green: dedupe with existing helpers, tighten types, simplify control flow.
   - Helpers like `_parser` and `_locate_function` currently live in both `analyses/call_graph.py` and `analyses/effects.py`. Extract them to a shared module when a *third* caller appears — not before.
   - Re-run `pytest -q && ruff check . && ruff format --check . && mypy src` before considering the cycle done.

### Conventions

- Tests live in `tests/unit/test_<module>.py` mirroring the source module name.
- Use `tmp_path` + a small `_write(repo, rel, body)` helper to build per-test fixtures rather than committing many fixture trees. `tests/unit/test_effects.py` is the canonical example.
- One behavior per test — failures should point to a specific contract violation, not a kitchen-sink dict comparison.
- When changing a public signature, **change the test first** (red). The failure mode should be the missing argument, not silently-passing tests.
- The `tests/fixtures/python_sample/` repo is the shared integration fixture. Add to it only when an integration-level behavior needs it — for unit tests, prefer `tmp_path`.

## Milestone-tag convention

The backlog is `grep -rn "milestone:\|STUB:" src/`. Tags come in two flavors:

| Tag style | Where it's used | Meaning |
|---|---|---|
| `NotImplementedError("milestone: <tag>")` | Module that *cannot* run yet (e.g. `analyses/cfg.py`, `sources/joern_source.py`). | Calling this raises. P0 callers must not invoke it. |
| `# STUB: <tag>` | Module that runs but returns a placeholder so downstream code can keep going (e.g. the historic `analyses/effects.py` stub). | Pipeline composes; output is documented placeholder, not real signal. |

Tag format is `P<tier>-<short-name>`. Current valid tiers are `P1` and `P2` (P0 is fully landed). Examples that exist today: `P1-cfg`, `P1-pdg`, `P2-joern-bridge`.

**Completing a milestone means the tag literally disappears from the grep.** Don't leave both real and stub code paths. If you need a temporary fallback, write a regular comment explaining why and a follow-up test that asserts the fallback can be removed.

## Code conventions

- Python 3.11+. Use `StrEnum`, `Annotated`, and `X | None` over older equivalents.
- mypy is `strict = true`. Public functions get full type hints; private helpers can lean on inference but should still type-hint anything load-bearing.
- Avoid speculative abstractions. The current scaffold deliberately repeats `_parser()` in two analysis modules because the third caller hasn't shown up yet (see "Refactor" step above).
- No `print` debugging in source. `typer.echo` is fine inside CLI commands.
- Keep docstrings to one short line unless the *why* is genuinely non-obvious. The module-level docstring is the right place for "this is what this layer does"; per-function docstrings should explain contracts, not restate signatures.

## Where things live

A reminder of the layout (see [`architecture.md`](./architecture.md) for the rationale):

```
src/cgir/
  cli.py              # the single pipeline driver; wire new passes here
  config.py           # CGIRConfig
  ir/                 # vocabulary (Node/Edge/RepoGraph/ComponentSpec)
  sources/            # GraphSource backends
  analyses/           # passes over RepoGraph
  slicing/            # Function/Method -> ComponentSpec
  export/             # write to disk
  trace/              # path:line lookup
  regenerate/         # prompt-pack + LLM (stub)
  api/                # FastAPI surface (stub)
tests/
  unit/test_<module>.py
  integration/test_cli_*.py
  fixtures/python_sample/   # shared integration fixture
schemas/              # published JSON schemas
docs/                 # this folder
```

If you're adding a file that doesn't fit one of the existing folders, that's usually a signal the architecture needs a sentence in [`architecture.md`](./architecture.md) — not a new folder.
