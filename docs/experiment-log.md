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

## Takeaway

The compression thesis holds *for the class of components whose contract is
its types* — pack ties file at an order of magnitude less context. It does
not hold for test-pinned semantics without the tests, nor for body-level
free names without closure. Both are addressable and scheduled. This is the
evidence base for continuing pack → verify → benchmark.

Cost: round 1 ~$0.25, round 2 ~$0.10 (Sonnet 4.6).
