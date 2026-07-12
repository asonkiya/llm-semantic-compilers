# Plan: 0.3 and beyond — trust, reach, and the developer loop

Written 2026-07-12 as a durable handoff: any session (any model) should be
able to pick up any thread from here. Prior art: `plan-0.2-0.4.md` (all
landed), `gate-noise.md` (measured rule noise), `experiment-log.md`
(benchmarks), `status.md` (what runs).

## Shipped in the 0.3 cook (see git log 2026-07-12)

1. **Hook UX** — blocked-commit message explains the staged-state trap
   (`git restore -S -W <file>`), found dogfooding.
2. **pre-commit framework integration** — `.pre-commit-hooks.yaml` so
   `repo: llm-semantic-compilers` works in anyone's `.pre-commit-config.yaml`.
3. **Agent onboarding** — `docs/agents.md`: the MCP config + CLAUDE.md/
   AGENTS.md snippet teaching agents the protocol (search → pack → edit →
   impact_of_change → verify).
4. **go.mod cross-package resolution** — ingest reads the `module` directive
   and stashes it on the Repository node; symbols strips the prefix from Go
   import targets and binds the alias to the target *package directory*
   (any module in it — the directory merge exposes all package members).
5. **Confidence tiers on effects** — every effect tag carries provenance:
   `high` (exact/prefix table match, import-alias verified) vs `lexical`
   (bare suffix / receiver-name heuristics: `.now`, db-receiver gating).
   New spec field `lexical_effects` (subset of `effects`). Gate rules
   (`effect-gain`/`effect-loss`) fire on high-confidence tags by default;
   `:any` suffix opts into lexical (e.g. `effect-gain:db:any`). This is the
   systematic fix for the `self.now()` false-positive class measured in
   gate-noise.md.

## Not built — next up, in value order

1. **Per-component incremental analysis** (the latency lever). Watch/hook
   re-scan everything (~0.5–4.5s). Design sketch: content-hash manifest
   identifies changed files → re-ingest only those modules → recompute
   analyses for changed components + their upstream closure
   (`compute_typed_impact` IS the scoping machinery) → splice into the
   cached graph. Correctness traps: effect closure is global (a leaf change
   can flip distant orchestrators — the closure must be re-run over the
   merged spec set, which is cheap since it's pure over specs); symbol
   tables must be rebuilt when imports/decls change, else only call-graph
   edges from changed files. Validate: randomized edit-replay equivalence
   (incremental result == full-scan result on N random historical edits).
2. **vitest/jest execution for `impact --run`** — runner detection from
   package.json; currently prints the command for TS.
3. **Go: struct-in-one-file/methods-in-another field DI** — the class-stub
   merge (registry of package-level types before method attachment).
4. **Data-shape v1.1** — full type text for Python fields (needs a second,
   non-DI fields channel), literal-key fingerprints for un-annotated dict
   returns, base-class field inheritance.
5. **LSP diagnostics** — watch already computes live drift; publish as
   editor diagnostics (pygls; diagnostics-only server, no completion).
6. **`cgir decompose`** — the spec's long-term flagship (PDG-sliced
   functional-core/imperative-shell suggestions + verify loop).
7. **Marketplace listing** for the Action; badges; a demo GIF in README.

## The developer loop (what "useful for me" means, concretely)

The user's repos and their CGIR state:
- **camera-tracking** (Py+TS): pins live on branch `cgir-pins`; pre-commit
  hook INSTALLED on the repo (uses PATH cgir = uv tool). `.mcp.json` +
  CLAUDE.md snippet added in this cook so agents use the semantic index.
- **Indra** (Go): scans clean (301 components). Candidate for pins next.
- **novel-chrome-extension** (TS/Angular): benchmark corpus, index at
  ~/.cgir-indexes/novel-frontend.

Daily loop: `cgir watch` in a terminal (live drift), agents route through
MCP (`pack`/`impact`/`verify` instead of grep), the hook gates commits, CI
gates PRs (Action), pins encode the invariants worth keeping.

## Release discipline

Tag-triggered trusted publishing (RELEASING.md). Bump BOTH version spots.
The maintainer pushes tags — releases are a human decision.
