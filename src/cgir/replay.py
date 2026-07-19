"""Rung 5: capture/replay behavioral oracle.

Contract equivalence is not behavioral equivalence, and random-input
synthesis can't build every input (opaque structs, ndarrays, ``Any``). So
instead of *inventing* inputs, *record real ones*: run the component's real
callers (its test suite, or any driver) with the target functions traced,
capture each ``(args, result)`` pair, then replay those recorded inputs
against a candidate and require identical outputs.

Two pieces:

- :func:`capture` runs a driver (default ``pytest``) in the repo with a
  ``setprofile`` tracer that records I/O for the requested functions —
  catching every real invocation regardless of import style.
- :func:`make_replay_oracle` returns a :data:`cgir.rewrite.BehavioralOracle`
  that execs a candidate in its module's namespace and replays the captured
  inputs, so it plugs straight into ``rewrite_repo(..., oracle=...)``.

Sound for pure functions (same input -> same output; argument mutation is
guarded by deep-copying replay inputs). The captured I/O is only as complete
as the driver's coverage — a function the driver never calls yields no
traces, which the oracle reports rather than silently passing.
"""

from __future__ import annotations

import dataclasses
import json
import math
import pickle
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

# A captured invocation: positional args snapshot + the return value.
Trace = tuple[tuple[Any, ...], Any]

_CAPTURE_HARNESS = """\
import copy, pickle, runpy, sys
from collections import defaultdict

TARGETS = set(tuple(t) for t in {targets!r})  # (abs_filename, func_name)
CAP = {cap!r}
records = defaultdict(list)
stack = []


def prof(frame, event, arg):
    if event == "call":
        code = frame.f_code
        key = (code.co_filename, code.co_name)
        if key in TARGETS:
            names = code.co_varnames[: code.co_argcount]
            try:
                snap = tuple(copy.deepcopy(frame.f_locals[n]) for n in names)
            except Exception:
                snap = None
            stack.append((key, snap))
        else:
            stack.append(None)
    elif event == "return":
        if stack:
            top = stack.pop()
            if top is not None and top[1] is not None:
                try:
                    records[top[0]].append((top[1], copy.deepcopy(arg)))
                except Exception:
                    pass


sys.argv = {driver_argv!r}
sys.setprofile(prof)
try:
    runpy.run_module({driver_module!r}, run_name="__main__", alter_sys=True)
except SystemExit:
    pass
finally:
    sys.setprofile(None)

out = {{}}
for (fn, name), traces in records.items():
    picklable = []
    for snap, ret in traces:
        try:
            pickle.dumps((snap, ret))
            picklable.append((snap, ret))
        except Exception:
            pass
    out[(fn, name)] = picklable
with open(CAP, "wb") as fh:
    pickle.dump(out, fh)
"""


def capture(
    repo: Path,
    targets: dict[str, tuple[Path, str]],
    driver_argv: list[str] | None = None,
    driver_module: str = "pytest",
    timeout: int = 900,
) -> dict[str, list[Trace]]:
    """Run ``driver_module`` (default pytest) in ``repo`` with the ``targets``
    traced, returning ``{qualname: [(args, result), ...]}``.

    ``targets`` maps qualname -> (source file, function name). Only picklable
    traces survive (so they can cross back from the subprocess); everything
    else is dropped rather than faked."""
    repo = repo.resolve()
    key_to_qual = {(str((repo / f).resolve()), name): q for q, (f, name) in targets.items()}
    workdir = Path(tempfile.mkdtemp(prefix="cgir-capture-"))
    cap = workdir / "traces.pkl"
    harness = workdir / "_cgir_capture.py"
    harness.write_text(
        _CAPTURE_HARNESS.format(
            targets=[list(k) for k in key_to_qual],
            cap=str(cap),
            driver_argv=driver_argv or [driver_module, "-q", "-p", "no:cacheprovider"],
            driver_module=driver_module,
        )
    )
    env_repo = str(repo)
    proc = subprocess.run(
        [sys.executable, str(harness)],
        cwd=env_repo,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if not cap.exists():
        raise RuntimeError(f"capture produced no traces (driver failed?):\n{proc.stderr[-1500:]}")
    # Captured values may be instances of the repo's own classes; make them
    # importable so the traces unpickle here (replay adds this too).
    if env_repo not in sys.path:
        sys.path.insert(0, env_repo)
    raw: dict[tuple[str, str], list[Trace]] = pickle.loads(cap.read_bytes())
    out: dict[str, list[Trace]] = {}
    for key, traces in raw.items():
        q = key_to_qual.get(key)
        if q is not None and traces:
            out[q] = traces
    return out


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return (math.isnan(a) and math.isnan(b)) or math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, int | float) and isinstance(b, int | float):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)
    if type(a) is not type(b):
        return False
    if dataclasses.is_dataclass(a) and not isinstance(a, type):
        return _eq(dataclasses.asdict(a), dataclasses.asdict(b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_eq(v, b[k]) for k, v in a.items())
    if isinstance(a, list | tuple):
        return len(a) == len(b) and all(_eq(x, y) for x, y in zip(a, b, strict=True))
    try:
        return bool(a == b)
    except Exception:
        return False


def _load_candidate(repo: Path, qualname: str, candidate: str) -> Callable[..., Any]:
    import copy
    import importlib

    if str(repo.resolve()) not in sys.path:
        sys.path.insert(0, str(repo.resolve()))
    module_name, func_name = qualname.rsplit(".", 1)
    mod = importlib.import_module(module_name)
    ns = dict(vars(mod))
    exec(compile(candidate, f"<candidate:{qualname}>", "exec"), ns)
    fn = ns[func_name]
    return lambda *a: fn(*copy.deepcopy(list(a)))


def replay(repo: Path, qualname: str, candidate: str, traces: list[Trace]) -> tuple[bool, str]:
    """Replay captured inputs against ``candidate``; ``(passed, feedback)``."""
    if not traces:
        return False, "no captured I/O to replay"
    try:
        fn = _load_candidate(repo, qualname, candidate)
    except Exception as exc:
        return False, f"candidate load error: {exc}"
    for args, expected in traces:
        try:
            got = fn(*args)
        except Exception as exc:
            return False, f"replay raised on {args!r}: {type(exc).__name__}: {exc}"
        if not _eq(got, expected):
            return False, f"replay mismatch on {args!r}: expected {expected!r}, got {got!r}"
    return True, ""


def make_replay_oracle(
    repo: Path, traces_by_qualname: dict[str, list[Trace]]
) -> Callable[[str, str], tuple[bool, str]]:
    """A :data:`cgir.rewrite.BehavioralOracle` backed by captured I/O — plug
    into ``rewrite_repo(..., oracle=make_replay_oracle(repo, traces))``."""

    def oracle(component_id: str, candidate: str) -> tuple[bool, str]:
        return replay(repo, component_id, candidate, traces_by_qualname.get(component_id, []))

    return oracle


def save_traces(traces_by_qualname: dict[str, list[Trace]], path: Path) -> None:
    """Persist captured traces (pickle — inputs may be arbitrary objects) with
    a JSON sidecar summary of counts for human inspection."""
    path.write_bytes(pickle.dumps(traces_by_qualname))
    summary = {q: len(t) for q, t in traces_by_qualname.items()}
    path.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2) + "\n")


def load_traces(path: Path) -> dict[str, list[Trace]]:
    return dict(pickle.loads(path.read_bytes()))
