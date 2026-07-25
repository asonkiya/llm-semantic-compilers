# verify-diff — a behavior-preservation gate for changes

You let an AI refactor a module, bumped a dependency, or hand-edited a hot path.
The question every LLM-assisted developer now has is: **did that actually change
behavior?** `cgir verify-diff` answers it against your repo's own tests — no clean
ABI, no fuzzer preconditions, no rewrite.

```bash
cgir verify-diff main            # compare the working tree to `main`
cgir verify-diff HEAD~1 --fuzz 5 # ...and fuzz around the recorded inputs
```

## How it works

1. **What changed** — diff the working tree against a base ref; find the Python
   functions whose source actually changed (top-level + one-level methods).
2. **Real inputs** — run your test suite once, recording the actual arguments each
   changed function is called with. Your tests *are* the workload; no synthetic
   input model.
3. **Differential** — run the **base** and **new** implementations on those same
   inputs (optionally plus edge-value mutations around them) and compare.
4. **Verdict** — every changed function lands in exactly one honest bucket:
   - `preserving` — agreed on every checked input,
   - `diverged` — a concrete input where they differ (printed as a counterexample),
   - `unverified` — couldn't check, with the reason (not covered by a test, doesn't
     parse). **Never a silent pass.**

Exit code is non-zero iff anything diverged, so it drops straight into CI as a
merge gate.

## In CI

```yaml
# .github/workflows/verify.yml
on: pull_request
jobs:
  behavior:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }          # base ref must exist
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install codegraph-ir && pip install -e .[test]
      - uses: ./.github/actions/verify-diff
        with:
          base-ref: origin/${{ github.base_ref }}
          fuzz: "5"
```

The action writes a PR-summary table (✅ preserving / ❌ diverged / ⚠️ unverified)
and fails the check on divergence.

## What it does and doesn't prove

- **Proves**: on every input your tests exercise (and edge mutations of them), the
  new code returns what the old code returned — or raises the same way. A real
  divergence comes with the exact input that triggers it.
- **Doesn't prove**: equivalence on inputs your tests never reach. `unverified` is
  reported honestly rather than dressed up as a pass; `--fuzz` widens coverage
  around what the tests do hit, but absence of a counterexample is not a proof of
  total equivalence. It is exactly as strong as your test suite, plus a margin —
  and it says so.

## Languages

**Python and JavaScript/TypeScript.** `cgir verify-diff` dispatches by file
extension and runs both in one pass on a mixed repo:

- **Python** — inputs recorded via a `sys.setprofile` hook over `pytest`; old/new
  run in-process.
- **JS/TS** — inputs recorded by a CommonJS `require` hook over `node --test`
  (mocha and plain node too; `jest`/`vitest` sandbox their own module registry and
  need a one-line setup wrap); old/new run in a Node harness, with TypeScript
  transpiled through the repo's own `esbuild` or `typescript@5`. Node is the only
  extra dependency — absent it, JS/TS changes are reported `unverified`, never
  silently skipped.

Both speak the identical three-bucket contract with the same value semantics
(float tolerance, NaN, deep structural equality). Other languages follow the same
source-binding seam as the rewrite pairs.
