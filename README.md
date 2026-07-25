# CodeGraph IR (CGIR)

**The deterministic contract layer for AI-modified codebases.** Agents write
more of the code than you can review. CGIR reads a repo (Python, TypeScript,
Go, Rust, C) and — with **zero LLM calls** — tells you what each component *is* (effects, purity, contract,
entrypoints, call surface) and whether a change *altered* it. Think **ruff,
but for architecture instead of style**: fast, static, hallucination-free.

Distributed as **`codegraph-ir`**; the command and import package are both
`cgir`.

## Install

```bash
uv tool install codegraph-ir      # isolated CLI (recommended); or: pipx install codegraph-ir
cgir --version
```

For library/agent use in a project: `uv pip install codegraph-ir`
(extras: `[mcp]` for the agent server, `[api]` for the HTTP surface,
`[llm]` for regeneration).

## The local loop

```bash
cgir scan .                          # build the .cgir index (Py, TS, Go, Rust, C)
cgir watch .                         # keep it live: re-scan + show contract drift on save
cgir pack app.service.charge --repo .   # minimal context bundle for one component
cgir impact app.service.charge          # blast radius: affected callers, entrypoints, tests
cgir impact app.service.charge --candidate new.py --repo .   # radius narrowed by the real delta
cgir verify app.service.charge --candidate new.py --repo .   # contract-check an edit
cgir verify-diff main                # prove changed functions are behavior-preserving vs a ref
cgir hook install                    # pre-commit seatbelt: block contract-breaking commits
cgir lsp                             # editor squiggles for pin violations + drift (cgir[lsp])
```

`pack` → edit → `impact` → `verify` → `hook`, with `watch` keeping the index
fresh underneath — an always-on membrane you and your agent both consult.

## Gate CI on contract drift

The [GitHub Action](./docs/github-action.md) scans a PR's base and head and
fails the build on drift — a pure function that starts hitting the network, a
service that *stops* persisting, a new `POST /admin` route — deterministically,
with no per-seat LLM cost:

```yaml
- uses: asonkiya/llm-semantic-compilers@v0
  with:
    fail-on: "effect-gain:net effect-gain:fs effect-gain:db effect-loss:net effect-loss:fs effect-loss:db"
```

The default rule set is [evidence-based](./docs/gate-noise.md): replaying real
commit history, the I/O effect rules fire on ~0–10% of commits, each a genuine
change in a component's I/O surface.

## Gate CI on behavior change too (`verify-diff`)

The contract gate above catches *structural* drift. `cgir verify-diff` catches
*behavioral* drift — did an AI refactor, a dependency bump, or a hand edit
actually change what a function does? It runs your test suite once to record each
changed function's real inputs, then runs the **old** and **new** code on those
inputs and reports a concrete counterexample if they differ:

```bash
cgir verify-diff main --fuzz 5
#   ✗ billing.apply_discount: diverged — on (100, 0.2): old returned 80, new returned 120
#   ✓ billing.round_cents: preserving — agreed on 14 inputs (+9 fuzzed)
#   ? billing.parse_currency: unverified — not exercised by the test suite
```

Every changed function lands in one honest bucket — `preserving` / `diverged`
(with the counterexample) / `unverified` (with the reason) — never a silent pass.
Works on **Python and JavaScript/TypeScript** (one pass, dispatched by extension).
Non-zero exit on divergence drops it straight into CI as a merge gate
([action](./.github/actions/verify-diff/action.yml), [docs](./docs/verify-diff.md)).
It is exactly as strong as your test suite, plus a fuzz margin — and it says so.

## Agents as first-class users

`cgir mcp --index .cgir` serves the index over MCP. Instead of grepping, an
agent calls `search` / `pack` to load minimal context, `impact` to see what a
change touches, and `verify` / `impact_of_change` to contract-check its own
edit before proposing it. See [`examples/`](./examples) for a worked
agent-PR case study.

Setup guide for agents (MCP config + CLAUDE.md snippet): [`docs/agents.md`](./docs/agents.md).
Or via the [pre-commit framework](https://pre-commit.com): hook id `cgir-contract-check`.

## How it compares (honestly)

| | **CGIR** | CodeGraph-style MCP graphs | Greptile / CodeRabbit | import-linter / ArchUnit | oasdiff / Pact |
|---|---|---|---|---|---|
| Effects & purity contracts per function | ✅ | ❌ | ❌ LLM judgment | ❌ | ❌ |
| Invariants declared in source (`# cgir: pure`) & enforced | ✅ | ❌ | ❌ | imports only | API boundary only |
| Deterministic (same input → same verdict, zero LLM) | ✅ | ✅ structure only | ❌ | ✅ | ✅ |
| Catches "service silently stopped calling the backend" | ✅ | ❌ | sometimes | ❌ | at spec boundaries |
| Blast radius + coverage-grounded test selection | ✅ | ❌ | ❌ | ❌ | ❌ |
| Agent context over MCP | ✅ contract packs | ✅ broader retrieval | ❌ | ❌ | ❌ |
| Finds logic bugs | ❌ | ❌ | ✅ | ❌ | ❌ |
| Languages | 5 builtin + [plugin API](./docs/languages.md) | 30+ | most | per-tool | spec-level |
| Cycle / layer rules | ✅ | ❌ | ❌ | ✅ mature | ❌ |
| Cost | free, local | mostly free | ~$24–30/user/mo | free | free |

Where CGIR loses, we say so: the *contract* gate does **not** find logic bugs (a
subtly wrong algorithm with unchanged effects passes it). `verify-diff` closes
part of that gap — it catches a *change* that alters behavior on any tested input
— but only as far as your tests reach; it can't judge brand-new code with no
baseline. CGIR supports 5 languages not 38, and its effect detection is static
analysis with [documented, measured limits](./docs/gate-noise.md) — every tag
carries a confidence tier so you know which claims are verified.

## Docs

- [`docs/strategy.md`](./docs/strategy.md) — positioning: the deterministic contract layer
- [`docs/status.md`](./docs/status.md) — what runs today, test coverage, milestones
- [`docs/gate-noise.md`](./docs/gate-noise.md) — false-alarm measurement on real history
- [`docs/github-action.md`](./docs/github-action.md) — CI contract-diff gate
- [`docs/verify-diff.md`](./docs/verify-diff.md) — the behavior-preservation gate for changes
- [`docs/experiment-log.md`](./docs/experiment-log.md) — rewrite-readiness / contract-preservation benchmarks
- [`docs/architecture.md`](./docs/architecture.md) — layered pipeline, data model, extension seams
- [`docs/languages.md`](./docs/languages.md) — adding a language (the `LanguageAdapter` seam)
- [`docs/design-rewrite-pairs.md`](./docs/design-rewrite-pairs.md) — the pluggable verified-rewrite engine (`cgir.rewrite_pairs`)
- [`Code-IR.md`](./Code-IR.md) — full product specification
- [`RELEASING.md`](./RELEASING.md) — how to cut a release

## License

MIT — see [`LICENSE`](./LICENSE).
