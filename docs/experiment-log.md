# Rewrite-readiness experiment log

Empirical grounding for the pack/verify direction. Method: for N components
of `camera-tracking`, remove the body and ask Sonnet 4.6 to reimplement it
from context, splice into a shadow repo, run the component's linked tests.
Two conditions: **pack** (CGIR contract bundle, no implementation) vs
**file** (whole source file with the body stubbed — the naive-agent
baseline). Harness: `scratchpad/rewrite_experiment.py`.

## Round 1 (Sprint 18 pack — spec + callee interfaces only)

| condition | pass | avg context |
|---|---|---|
| pack | 4/12 | ~219 tok |
| file | 8/12 | ~3,360 tok |

Failures were **missing data shapes, not missing algorithms**: the model
reconstructed ray casting and OAuth flows correctly but guessed `p.x`
where `Point` is a tuple, missed config-dict keys, missed module constants.
Diagnosis → enrich the pack with type closure, docstrings, raises.

## Round 2 (Sprint 23 pack — + type closure, docstrings, raises, aliases)

| condition | pass | avg context |
|---|---|---|
| pack | **6/12** | ~266 tok |
| file | 8/12 | ~3,360 tok |

The two flips (`point_in_polygon`, `joint_angle`) are exactly the
type-shape failures the enrichment targeted — including
`Point: TypeAlias = tuple[float, float]` in the bundle made the model
unpack correctly. **Pack now matches file on the type-shape class at ~13x
less context.** `get_summary`: 141 tok (pack) vs 20,198 (file), both pass.

The residual 6 failures split cleanly:

1. **Semantics pinned only by tests** (`update_iou_tracker`, `validate`,
   `topo_sort`) — fail under *file too*. No context of surrounding *code*
   reveals exact expiry behavior / tie-break order / error attributes.
   Fix: linked test source in the pack (Sprint 25) or `algorithm` bullets.
2. **Body-level free-name closure** (`default_pipeline` node constants,
   `authorize_url` config keys, `get_daily_rollup` snapshot fields) —
   file passes, pack fails. Current closure pulls names from the *signature/
   return* types only; these need free names referenced in the *body*.
   Fix: extend module-constant closure to body free-references.

## Round 3 (Sprint 25 pack — + linked test source via covered_by)

| condition | pass | avg context |
|---|---|---|
| pack | **8/12** | ~470 tok |
| file | 8/12 | ~3,360 tok |

**Pack now ties the full-file baseline at 1/7th the context** — and passes
three components the file condition *fails* (`update_iou_tracker`,
`zones_for_points`, `topo_sort`). Those are test-pinned semantics: the
linked tests encode the exact expiry behavior / tie-break order that the
surrounding *code* never states. The contract bundle is, for that class,
**better than the raw file, not just smaller.**

Progression as enrichment landed: **4 → 6 → 8 / 12**.

Remaining 4 pack failures:
- `validate` — fails under file too (semantics beyond types+tests in a
  single-function splice).
- `default_pipeline`, `get_daily_rollup`, `authorize_url` — file passes,
  pack fails. All in the **body free-name closure** bucket: module
  constants / config-dict keys referenced in the *body*, not the signature.
  This is the one scoped, un-built enrichment left.

## Round 4 (Sprint 27 pack — + body free-name closure)

| condition | pass | avg context |
|---|---|---|
| pack | **9/12** | ~820 tok |
| file | 8/12 | ~3,360 tok |

Same-module constants and small helpers the *body* references are now
included (e.g. `_cfg()`'s body reveals the config-dict keys that
`authorize_url` reads). `authorize_url` flipped to pass. **Pack now
*exceeds* the full-file baseline at ~4x less context.**

Final progression: **4 → 6 → 8 → 9 / 12**.

Residual 3:
- `validate` — fails under file too; genuinely hard in a single-function
  splice (interdependent validation of a graph structure).
- `default_pipeline` — the test pins an exact template structure the model
  can't reproduce without seeing the template itself (a data fixture, not
  code the contract names).
- `get_daily_rollup` — the linked test asserts on a snapshot's exact
  field shape produced elsewhere; needs cross-component fixture context.

All three are "the answer is a specific data structure defined elsewhere,"
not a contract-comprehension gap.

## Takeaway

Monotonic evidence (4→6→8→9): **an enriched contract bundle beats full-file
context at ~4x less** — matching or exceeding it by including exactly the
semantic pieces CGIR identifies (types, linked tests, module context)
rather than dumping the file. The remaining failures are exact-data-fixture
cases, not comprehension gaps. This is the evidence base for the
pack → verify → gate loop.

Cost: rounds 1–4 ~$0.55 total (Sonnet 4.6).
