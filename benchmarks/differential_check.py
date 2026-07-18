"""Differential equivalence check for rung-3 winners (the rung-5 seed).

For each solved component in a rung3_rewrite results file, run the *winning
candidate* and the *original* side by side on randomly synthesized inputs
(depth-limited generation from type annotations) and compare outputs.
Sound for this worklist because the components are pure functions: same
input must give same output, and argument mutation is checked separately
via deep-copied inputs.

This measures what the gating stage could NOT: the false-pass rate of a
filter on components with no tests. Components whose signatures we can't
synthesize (Any, ndarray, unresolvable hints) are reported as such, never
silently counted.

Run with the target repo's interpreter:
    <target>/.venv/bin/python benchmarks/differential_check.py \
        --results benchmarks/rung3-uncovered-camera-tracking.json \
        --repo <target> --n 300
"""

from __future__ import annotations

import argparse
import collections.abc as cabc
import copy
import dataclasses
import enum
import importlib
import json
import math
import random
import sys
import types
import typing
from pathlib import Path
from typing import Any


class Unsynthesizable(Exception):
    pass


_MAX_DEPTH = 5


def _synth(tp: Any, rng: random.Random, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        raise Unsynthesizable(f"depth limit at {tp!r}")
    origin, targs = typing.get_origin(tp), typing.get_args(tp)

    if tp is type(None) or tp is None:
        return None
    if tp is int:
        return rng.randint(-100, 100)
    if tp is float:
        return rng.choice([0.0, round(rng.uniform(-100.0, 100.0), 3)])
    if tp is bool:
        return rng.random() < 0.5
    if tp is str:
        return "".join(rng.choice("abcdefgh_xyz") for _ in range(rng.randint(0, 8)))
    if tp is list:
        tp, origin, targs = list[str], list, (str,)
    if tp is dict:
        tp, origin, targs = dict[str, str], dict, (str, str)
    if origin in (list, cabc.Sequence, cabc.Iterable, cabc.MutableSequence):
        elem = targs[0] if targs else str
        return [_synth(elem, rng, depth + 1) for _ in range(rng.randint(0, 6))]
    if origin in (set, frozenset):
        elem = targs[0] if targs else str
        try:
            return {_synth(elem, rng, depth + 1) for _ in range(rng.randint(0, 5))}
        except TypeError as exc:  # unhashable synthesized element
            raise Unsynthesizable(f"unhashable set element {elem!r}") from exc
    if origin in (dict, cabc.Mapping, cabc.MutableMapping):
        kt, vt = targs if len(targs) == 2 else (str, str)
        return {
            _synth(kt, rng, depth + 1): _synth(vt, rng, depth + 1) for _ in range(rng.randint(0, 5))
        }
    if origin is tuple:
        if len(targs) == 2 and targs[1] is Ellipsis:
            return tuple(_synth(targs[0], rng, depth + 1) for _ in range(rng.randint(0, 5)))
        return tuple(_synth(t, rng, depth + 1) for t in targs) if targs else ()
    if origin in (typing.Union, types.UnionType):
        return _synth(rng.choice(targs), rng, depth + 1)
    if origin is typing.Literal:
        return rng.choice(targs)
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        return rng.choice(list(tp))
    if dataclasses.is_dataclass(tp) and isinstance(tp, type):
        hints = typing.get_type_hints(tp)
        kwargs = {}
        for f in dataclasses.fields(tp):
            if not f.init:
                continue
            kwargs[f.name] = _synth(hints.get(f.name, str), rng, depth + 1)
        return tp(**kwargs)
    if isinstance(tp, type) and hasattr(tp, "__required_keys__"):  # TypedDict
        hints = typing.get_type_hints(tp)
        return {k: _synth(v, rng, depth + 1) for k, v in hints.items()}
    raise Unsynthesizable(repr(tp))


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
    if (
        isinstance(a, int | float)
        and isinstance(b, int | float)
        and not (isinstance(a, bool) is not isinstance(b, bool))
    ):
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


def _call(fn: Any, args: list[Any]) -> tuple[str, Any]:
    try:
        return "ok", fn(*copy.deepcopy(args))
    except Exception as exc:
        return "exc", type(exc).__name__


def check_component(cid: str, candidate_src: str, repo: Path, n: int, seed: int) -> dict[str, Any]:
    module_name, func_name = cid.rsplit(".", 1)
    out: dict[str, Any] = {"component_id": cid, "status": "", "trials": 0, "mismatches": 0}
    try:
        mod = importlib.import_module(module_name)
    except Exception as exc:
        out["status"] = f"import_error: {type(exc).__name__}: {exc}"
        return out
    original = getattr(mod, func_name)
    ns = dict(vars(mod))
    try:
        exec(compile(candidate_src, f"<candidate:{cid}>", "exec"), ns)
    except Exception as exc:
        out["status"] = f"candidate_exec_error: {exc}"
        return out
    cand = ns[func_name]

    try:
        hints = typing.get_type_hints(original)
    except Exception as exc:
        out["status"] = f"unsynthesizable: hints failed ({exc})"
        return out
    import inspect

    params = [p for p in inspect.signature(original).parameters.values()]
    if any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in params):
        out["status"] = "unsynthesizable: *args/**kwargs"
        return out
    if any(p.name not in hints for p in params):
        out["status"] = "unsynthesizable: unannotated parameter"
        return out

    rng = random.Random(seed)
    example = None
    for trial in range(n):
        try:
            args = [_synth(hints[p.name], rng) for p in params]
        except Unsynthesizable as exc:
            out["status"] = f"unsynthesizable: {exc}"
            return out
        kind_o, val_o = _call(original, args)
        kind_o2, val_o2 = _call(original, args)
        if kind_o != kind_o2 or (kind_o == "ok" and not _eq(val_o, val_o2)):
            out["status"] = "nondeterministic_original"
            return out
        kind_c, val_c = _call(cand, args)
        out["trials"] = trial + 1
        same = kind_o == kind_c and (kind_o == "exc" or _eq(val_o, val_c))
        if not same:
            out["mismatches"] += 1
            if example is None:
                example = {
                    "args": repr(args)[:400],
                    "original": f"{kind_o}: {val_o!r}"[:300],
                    "candidate": f"{kind_c}: {val_c!r}"[:300],
                }
    out["status"] = "equivalent" if out["mismatches"] == 0 else "mismatch"
    if example:
        out["example"] = example
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--arm", default=None)
    args = ap.parse_args()

    sys.path.insert(0, str(args.repo))
    report = json.loads(args.results.read_text())
    rows = []
    for res in report["results"]:
        if not res["solved_by"] or (args.arm and res["arm"] != args.arm):
            continue
        winner = res["attempts"][-1]
        assert winner["gate_ok"], res["component_id"]
        row = check_component(
            res["component_id"], winner["candidate"], args.repo, args.n, args.seed
        )
        row["arm"] = res["arm"]
        row["solved_by"] = res["solved_by"]
        rows.append(row)
        print(f"{row['status']:<40s} {res['component_id']}", flush=True)

    counts: dict[str, int] = {}
    for row in rows:
        key = row["status"].split(":")[0]
        counts[key] = counts.get(key, 0) + 1
    summary = {"n_checked": len(rows), "verdicts": counts, "trials_per_component": args.n}
    out_path = args.out or args.results.with_name(args.results.stem + "-differential.json")
    out_path.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
