# Gate noise: how often would `cgir diff` false-alarm on real history?

A gate that cries wolf gets disabled. Before recommending a default
`--fail-on` set, we replayed real commit history through the gate and
counted how often each rule fires — and inspected the fires to separate
genuine regressions from noise.

**Method.** For every commit that touches source, extract the parent tree
and the commit tree (`git archive`), scan both, `compute_diff`, and record
which `--fail-on` rules trip. Harness: `scratchpad/noise_replay.py`. Two
real repos, chosen for mature history in each supported language.

## Results

### camera-tracking (Python, 63 commits analyzed)

A real vision/ML + backend codebase. 38% of commits tripped *some* rule —
but the breakdown is the whole story:

| rule | commits fired | total hits | verdict |
|---|---|---|---|
| `entrypoint-added` | 16 (25%) | 84 | **noise** — adding an endpoint is normal |
| `kind-change` | 10 (16%) | 30 | **mostly noise** — see below |
| `purity-drop` | 5 (8%) | 14 | **mostly noise** — co-fires w/ kind-change |
| `effect-loss:net` | 2 | 4 | real change (a refactor) |
| `effect-loss:fs` | 2 | 2 | 1 real, 1 indirection artifact |
| `effect-gain:nondeterm` | 2 | 2 | low-signal (a timestamp/uuid) |
| `effect-gain:fs` | 1 | 1 | real change |

Restricting to the **I/O effect rules** (`effect-gain`/`effect-loss` on
`net`/`fs`/`db`) collapses the fire rate from 38% to **~10%**, and every
one points at a genuine change in what a component talks to.

### omniconvert (TypeScript, 60 recent commits analyzed)

A browser-side file-format converter (261 components, real effect spread:
net 5, fs 2, io 15, raise 51). **0% of commits tripped any rule.** The
codebase is mostly pure transforms and grows by *adding* new format
handlers (new components don't trip drift rules, by design), so the gate
stays completely silent until an existing component's I/O actually changes.
Exactly the desired behavior.

## What the noise is made of

Inspecting the Python fires:

- **`entrypoint-added` is not a defect signal.** It fires whenever a new
  route/CLI command appears — routine feature work. Belongs in a *report*,
  never a build failure. It alone produced 84 of the 137 total hits.
- **`kind-change` + `purity-drop` co-fire and are import-sensitive.** The
  worst offenders were commits that swapped a `cv2` test stub for the real
  library (`8eab30a6`, `795fd287`): functions calling into the stub classify
  as pure, into the real lib as effectful, so *the same source* flips kind
  and purity when only an import resolved differently. That's a precision
  limitation, not a code regression.
- **The I/O rules are quiet and meaningful — but "meaningful" ≠ "bug".**
  `aea106ec` lost `net` on three UI components because an auth refactor
  centralized network calls into an API layer. A real contract change, a
  *legitimate* refactor. So even the good rules should gate as
  "review-required," not a hard permanent block.
- **One genuine false alarm class: indirection.** `1373e643` shows
  `run_stgcn_actions: ['fs','io'] → ['calls_effectful','io']` — the file
  read moved *behind a call*, gaining `calls_effectful`. The effect wasn't
  removed, just indirected, yet `effect-loss:fs` fired.

## Recommended default gate

Evidence-based, low-noise:

```yaml
fail-on: "effect-gain:net effect-gain:fs effect-gain:db effect-loss:net effect-loss:fs effect-loss:db"
```

~0–10% fire rate on real history, and each fire is a real change in a
component's I/O surface — the thing worth a human's eyes on an
agent-written PR. Treat a hit as **review-required**, not a hard block.

Keep **report-only** (surfaced in the PR comment, never failing the build):
`entrypoint-added`, `entrypoint-change`, `kind-change`, `purity-drop`, and
the `io` / `nondeterm` effect tags (a `print`/timestamp shouldn't fail CI).

## Follow-ups this surfaced

1. **Indirection-aware `effect-loss`** *(landed)* — an `effect-loss:<tag>`
   is suppressed when the component simultaneously gains `calls_effectful`:
   the effect moved behind a call and is still transitively reachable, it
   didn't disappear. The `1373e643` false alarm above no longer fires.
   Trade-off (documented in `diff.violations`): a true removal paired with a
   new unrelated effectful call is masked — the loss stays visible in the
   diff report, it just doesn't fail the build.
2. **Import-resolution stability** — kind/purity flipping on stub-vs-real
   imports is the dominant `kind-change`/`purity-drop` noise source. A
   scan run compares two checkouts in one environment (CI does this), so it
   only bites when the stub *source itself* changes — but it's worth
   flagging as a precision limit.
