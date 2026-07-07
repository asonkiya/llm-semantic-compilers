# CGIR contract diff — GitHub Action

Fail a PR when it silently changes what your code *is*: a pure function
that gains a `net` call, a route that starts touching the filesystem, a
new `POST /admin` endpoint nobody flagged. Deterministic, static, zero
per-seat LLM cost — a linter for architecture, not an opinion.

## Quick start

```yaml
# .github/workflows/cgir.yml
name: CGIR contract diff
on: pull_request

permissions:
  contents: read
  pull-requests: write   # for the PR comment

jobs:
  contract-diff:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0    # base ref must be reachable
      - uses: your-org/cgir@v0     # or: local ./ during dogfooding
        with:
          # evidence-based low-noise default — see docs/gate-noise.md
          fail-on: "effect-gain:net effect-gain:fs effect-gain:db effect-loss:net effect-loss:fs effect-loss:db"
```

## What it does

1. Scans the PR base commit and the head, producing two CGIR indexes.
2. `cgir diff --markdown` between them.
3. Posts the diff as a PR comment **and** to the job summary.
4. Fails the job if any `fail-on` rule matches.

## Inputs

| input | default | meaning |
|---|---|---|
| `paths` | `.` | Subpaths to scan (monorepo: `services/a services/b`). |
| `exclude` | – | Directory names to skip. |
| `fail-on` | – (report only) | Space-separated drift rules; any match fails the build. |
| `comment` | `true` | Post a PR comment (needs `pull-requests: write`). |
| `cgir-version` | `cgir` | pip spec to install. |

## Fail rules

| rule | fires when |
|---|---|
| `effect-gain` | an existing component gains any effect tag |
| `effect-gain:<tag>` | …gains a specific tag (`net`, `fs`, `db`, `io`, `nondeterm`, `raise`) |
| `effect-loss` / `effect-loss:<tag>` | …*loses* an effect tag (e.g. a `create` that stops POSTing) |
| `purity-drop` | purity score decreases |
| `kind-change` | component kind changes (e.g. `pure_function` → `effect_adapter`) |
| `entrypoint-added` | a new HTTP route / CLI command / task appears |
| `entrypoint-change` | an existing entrypoint's path/method changes |

Rules that inspect existing components fire only when the component is
present in **both** base and head — new effectful code is a deliberate
choice, drift in existing code is a regression.

## Choosing a policy

We replayed real commit history through the gate to measure false-alarm
rate per rule ([`docs/gate-noise.md`](./gate-noise.md)). Summary:

- **Low-noise (recommended default):** `effect-gain`/`effect-loss` on
  `net`/`fs`/`db`. ~0–10% of real commits fire, and each is a genuine
  change in a component's I/O surface. Treat a hit as *review-required*.
- **Report-only (noisy — surface, don't fail):** `entrypoint-added`
  (fires on every new endpoint — 25% of commits), `kind-change` /
  `purity-drop` (import-sensitive, co-fire), and the `io` / `nondeterm`
  tags (a `print`/timestamp shouldn't fail CI).

```yaml
# recommended: nothing new reaches — or silently stops reaching — the outside world
fail-on: "effect-gain:net effect-gain:fs effect-gain:db effect-loss:net effect-loss:fs effect-loss:db"

# minimal: the pure core stays pure, nothing new hits the network
fail-on: "effect-gain:net effect-loss:net"

# strict (expect noise — see gate-noise.md): any contract movement is reviewed
fail-on: "effect-gain effect-loss purity-drop kind-change entrypoint-added entrypoint-change"
```

## Notes

- Requires `fetch-depth: 0` so the base commit is available to scan.
- The job summary always shows the diff even without comment permissions.
- Cross-version base/head comparisons emit a schema-mismatch warning
  (pin `cgir-version` to avoid it).
