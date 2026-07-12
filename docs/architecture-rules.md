# Architecture rules — `cgir lint`

Import linters ([Tach](https://github.com/tach-org/tach),
[import-linter](https://import-linter.readthedocs.io/)) constrain *imports*.
`cgir lint` constrains *meaning* — which components may carry which
effects, what kind they must be, and what they may call. It sees that a
function touches the network or routes to the DB layer, which an import
graph cannot.

Deterministic and free per run: pairs with the [contract-diff
Action](./github-action.md) as the standing policy layer.

## Rules file (`cgir.toml`)

```toml
# The geometry core is pure math — no I/O of any kind.
[[rule]]
name = "pure-geometry"
in = "aspen.zones.*"
forbid-effect = ["net", "fs", "db", "io"]

# Trackers must be pure functions.
[[rule]]
name = "trackers-pure"
in = "aspen.tracking.*"
require-kind = "pure_function"

# The actions layer must not reach into the API/HTTP layer.
[[rule]]
name = "no-actions-to-api"
in = "aspen.actions.*"
forbid-call = "aspen.api.*"
```

Each `[[rule]]` is scoped by an `in` id-glob and carries one predicate:

| predicate | fails when a matched component… |
|---|---|
| `forbid-effect = [tags]` | carries any of these effect tags |
| `require-kind = "kind"` | is not that ComponentKind |
| `forbid-call = "glob"` | calls a component whose id matches the glob |

## Usage

```bash
cgir scan . --out .cgir
cgir lint --index .cgir --config cgir.toml   # exit 1 on any violation
cgir lint --index .cgir --json               # machine-readable
```

`--config` defaults to `cgir.toml`. Globs use `fnmatch` over the dotted
component id (`aspen.zones.*` matches `aspen.zones.contains.point_in_polygon`).

## Why it's different

- **Effect-aware**: "the pure core must have no `fs`" is invisible to an
  import linter — the import of `pathlib` isn't the violation, *using* it is.
- **Kind-aware**: "handlers must be orchestrators, not effect adapters."
- **Call-target-aware**: layer boundaries over *resolved calls*, catching
  a cross-layer call even when the import is indirect.

## Cycles and layers

```toml
[[rule]]
name = "core is acyclic"
in = "app.*"
forbid-cycle = true          # Tarjan SCCs over resolved CALLS; self-recursion OK

[[rule]]
name = "layered architecture"
layers = ["app.api.*", "app.core.*", "app.db.*"]   # top -> bottom
```

`forbid-cycle` fires once per strongly-connected component (size >= 2),
naming the cycle. `layers` requires dependencies to point *downward*:
a `db` component calling an `api` component is a violation; same-layer and
layer-skipping downward calls are fine; components matching no layer are
ignored. Unlike import linters, these run over the resolved *call* graph —
and the same index tells you whether the violating edge also carries `net`.
