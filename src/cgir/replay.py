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
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

# A captured invocation: positional args snapshot + the outcome. The outcome is
# the return value, or {_RAISE_KEY: exc_type_name} when the call exited by
# raising — a raising call is NOT a call that returned None.
Trace = tuple[tuple[Any, ...], Any]

_RAISE_KEY = "__cgir_raise__"


def _is_raise_marker(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {_RAISE_KEY}


# settrace, not setprofile: the profiler's `return` event fires with arg=None
# both for `return None` and for an exception exit — indistinguishable, and
# sys.exc_info() is empty during unwind, so a raising call used to be recorded
# as (args, None). The tracer disambiguates via event order: an `exception`
# still pending at `return` means a raising exit; a `line` event after it means
# the frame caught it and kept executing. Only target frames get a local trace,
# so overhead stays close to the profiler version.
_CAPTURE_HARNESS = """\
import copy, os, pickle, runpy, sys
from collections import defaultdict

# The harness file lives in a tempdir, so sys.path[0] is that tempdir, not the
# repo — pytest run via runpy then can't import the repo's own package by name
# ("module X has no attribute ..."), collecting nothing. Put the repo cwd first,
# exactly as a normal `pytest` invocation in the repo would.
sys.path.insert(0, os.getcwd())

TARGETS = set(tuple(t) for t in {targets!r})  # (abs_filename, in-file qualname)
CAP = {cap!r}
RAISE_KEY = {raise_key!r}
# Loop machinery can surface these as trace `exception` events without a
# visible raise; if one is still pending at return-with-None the exit is
# ambiguous, so the trace is dropped rather than guessed.
AMBIGUOUS = ("StopIteration", "StopAsyncIteration", "GeneratorExit")
records = defaultdict(list)
snaps = {{}}    # id(frame) -> deep-copied args
pending = {{}}  # id(frame) -> in-flight exception type name


def _local(frame, event, arg):
    fid = id(frame)
    if event == "exception":
        pending[fid] = arg[0].__name__
        frame.f_trace_lines = True  # so a catch inside the frame clears it
    elif event == "line":
        pending.pop(fid, None)
        frame.f_trace_lines = False
    elif event == "return":
        code = frame.f_code
        snap = snaps.pop(fid, None)
        exc = pending.pop(fid, None)
        if snap is None:
            return _local
        try:
            if exc is None:
                out = copy.deepcopy(arg)
            elif arg is None and exc not in AMBIGUOUS:
                out = {{RAISE_KEY: exc}}
            else:
                return _local  # ambiguous exit: drop, never fake
            records[(code.co_filename, code.co_qualname)].append((snap, out))
        except Exception:
            pass
    return _local


def _global(frame, event, arg):
    if event != "call":
        return None
    code = frame.f_code
    if (code.co_filename, code.co_qualname) not in TARGETS:
        return None
    names = code.co_varnames[: code.co_argcount]
    try:
        snaps[id(frame)] = tuple(copy.deepcopy(frame.f_locals[n]) for n in names)
    except Exception:
        pass
    frame.f_trace_lines = False
    return _local


sys.argv = {driver_argv!r}
sys.settrace(_global)
try:
    runpy.run_module({driver_module!r}, run_name="__main__", alter_sys=True)
except SystemExit:
    pass
finally:
    sys.settrace(None)

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

    ``targets`` maps qualname -> (source file, in-file qualname) — the in-file
    qualname is ``co_qualname`` (``f`` for a function, ``Cls.f`` for a method),
    so two same-named methods in one file stay distinct. Only picklable traces
    survive (so they can cross back from the subprocess); everything else is
    dropped rather than faked."""
    repo = repo.resolve()
    key_to_qual = {(str((repo / f).resolve()), name): q for q, (f, name) in targets.items()}
    workdir = Path(tempfile.mkdtemp(prefix="cgir-capture-"))
    cap = workdir / "traces.pkl"
    harness = workdir / "_cgir_capture.py"
    harness.write_text(
        _CAPTURE_HARNESS.format(
            targets=[list(k) for k in key_to_qual],
            cap=str(cap),
            raise_key=_RAISE_KEY,
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
    # importable so the traces unpickle here. Evict any same-named module
    # cached from another path first — unpickling against a foreign module
    # fails (or worse, resolves to the wrong class).
    if env_repo not in sys.path:
        sys.path.insert(0, env_repo)
    for target_qual in targets:
        module_name, _chain = _split_qualname(repo, target_qual)
        top = module_name.split(".", 1)[0]
        mod = sys.modules.get(top)
        origin = getattr(mod, "__file__", None) if mod is not None else None
        if mod is not None and (
            origin is None or not str(Path(origin).resolve()).startswith(env_repo)
        ):
            for k in [k for k in sys.modules if k == top or k.startswith(top + ".")]:
                del sys.modules[k]
    raw: dict[tuple[str, str], list[Trace]] = pickle.loads(cap.read_bytes())
    out: dict[str, list[Trace]] = {}
    for key, traces in raw.items():
        q = key_to_qual.get(key)
        if q is not None and traces:
            out[q] = traces
    shutil.rmtree(workdir, ignore_errors=True)  # kept on failure paths for debugging
    return out


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return (math.isnan(a) and math.isnan(b)) or math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    # Ints compare exactly — isclose(float(a), float(b)) collapses values past
    # 2**53 (hashes, IDs, checksums), exactly where the fuzzer's edge values
    # probe. int-vs-float is an observable type change, not a rounding matter.
    if isinstance(a, int) and isinstance(b, int):
        return a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return False
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


def _import_repo_module(repo: Path, module_name: str) -> Any:
    """Import ``module_name`` ensuring it comes from THIS repo. A same-named
    module cached in ``sys.modules`` from another path would otherwise be
    silently substituted — the candidate would exec against a stranger's
    globals, and captured instances would fail to unpickle. Foreign entries
    are evicted so the repo-local version loads."""
    import importlib

    repo_s = str(repo.resolve())
    if repo_s not in sys.path:
        sys.path.insert(0, repo_s)
    top = module_name.split(".", 1)[0]
    mod = sys.modules.get(top)
    origin = getattr(mod, "__file__", None) if mod is not None else None
    if mod is not None and (origin is None or not str(Path(origin).resolve()).startswith(repo_s)):
        for k in [k for k in sys.modules if k == top or k.startswith(top + ".")]:
            del sys.modules[k]
        importlib.invalidate_caches()
    return importlib.import_module(module_name)


def _split_qualname(repo: Path, qualname: str) -> tuple[str, list[str]]:
    """Split ``module.Class.method`` into (module name, attribute chain) by
    probing the filesystem — the longest prefix that is a module file or
    package wins. ``rsplit('.', 1)`` guessed wrong for methods: it tried to
    ``import module.Class``, so no changed method could ever be verified."""
    parts = qualname.split(".")
    for i in range(len(parts) - 1, 0, -1):
        rel = Path(*parts[:i])
        if (repo / rel.with_suffix(".py")).exists() or (repo / rel / "__init__.py").exists():
            return ".".join(parts[:i]), parts[i:]
    return ".".join(parts[:-1]), [parts[-1]]


def _load_candidate(repo: Path, qualname: str, candidate: str) -> Callable[..., Any]:
    import copy
    import textwrap

    module_name, chain = _split_qualname(repo, qualname)
    mod = _import_repo_module(repo, module_name)
    ns = dict(vars(mod))
    # dedent: a method's source segment arrives indented, which compile rejects.
    exec(compile(textwrap.dedent(candidate), f"<candidate:{qualname}>", "exec"), ns)
    fn = ns[chain[-1]]
    return lambda *a: fn(*copy.deepcopy(list(a)))


def _load_from_module_source(repo: Path, qualname: str, module_source: str) -> Callable[..., Any]:
    """Resolve ``qualname`` from a full module source (e.g. the base-ref version
    of the file), so the function executes against ITS OWN module state —
    constants, helpers, class bodies as they were — not the working tree's.
    Imports of *other* modules still resolve to their working-tree versions;
    cross-module drift is a documented residual limit, not silently masked
    same-file drift."""
    import copy
    import types

    if str(repo.resolve()) not in sys.path:
        sys.path.insert(0, str(repo.resolve()))
    module_name, chain = _split_qualname(repo, qualname)
    mod = types.ModuleType(module_name)
    mod.__file__ = f"<cgir-old:{module_name}>"
    if "." in module_name:
        mod.__package__ = module_name.rsplit(".", 1)[0]
    exec(compile(module_source, mod.__file__, "exec"), mod.__dict__)
    obj: Any = mod
    for name in chain:
        obj = getattr(obj, name)
    fn = obj
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
        # SystemExit (argparse / sys.exit in a CLI function) is a BaseException,
        # not Exception — catching only Exception let it escape and crash the
        # whole rewrite run. A candidate that exits is a failed replay, not a
        # harness death. KeyboardInterrupt is deliberately NOT swallowed.
        except (Exception, SystemExit) as exc:
            if _is_raise_marker(expected):
                if type(exc).__name__ == expected[_RAISE_KEY]:
                    continue
                return False, (
                    f"replay raised {type(exc).__name__} on {args!r}, "
                    f"original raised {expected[_RAISE_KEY]}"
                )
            return False, f"replay raised on {args!r}: {type(exc).__name__}: {exc}"
        if _is_raise_marker(expected):
            return False, (
                f"replay returned {got!r} on {args!r}, original raised {expected[_RAISE_KEY]}"
            )
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
