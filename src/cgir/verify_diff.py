"""verify-diff — prove a change is behavior-preserving, against the repo's own tests.

The developer-facing counterpart to the rewrite engine: instead of *generating* a
new implementation and verifying it, take a change that already exists (an AI
refactor, a dependency bump, a hand edit) and answer the question every
LLM-assisted developer now has — *did this actually change behavior?*

The oracle is the repo's own test suite (no scalar ABI, no fuzzer preconditions):
run the tests once to record the real inputs each changed function is called with,
then run the **old** and **new** implementations on those same inputs and compare.
Divergence on a tested input is a behavior change, reported with a counterexample.
Optionally fuzz *around* the recorded inputs to catch divergences the tests don't
directly exercise.

Every changed function lands in exactly one honest bucket:
  * ``preserving`` — old and new agreed on every checked input,
  * ``diverged``   — a concrete input where they differ (the counterexample),
  * ``unverified`` — couldn't check, with the specific reason (not covered by a
                     test, doesn't parse, load error). Never a silent pass.
"""

from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cgir.replay import Trace, _eq, _load_candidate

PRESERVING = "preserving"
DIVERGED = "diverged"
UNVERIFIED = "unverified"


@dataclass
class Verdict:
    qualname: str
    status: str  # PRESERVING | DIVERGED | UNVERIFIED
    detail: str
    checked: int = 0  # inputs on which old and new were compared
    fuzzed: int = 0  # of those, how many were fuzzed (not directly recorded)


@dataclass
class ChangedFunction:
    qualname: str  # module.func or module.Class.method
    path: str  # repo-relative
    func_name: str  # bare name (for trace capture targets)
    old_src: str  # source at the base ref
    new_src: str  # source in the working tree


# --- the core differential: two implementations, same inputs ----------------


def differential_replay(
    repo: Path,
    qualname: str,
    old_src: str,
    new_src: str,
    arg_tuples: list[tuple[Any, ...]],
) -> Verdict:
    """Run ``old_src`` and ``new_src`` (each a function definition) on every
    input in ``arg_tuples`` and require identical results (or identically-typed
    exceptions). Returns the first divergence as a counterexample, else
    ``preserving``. Reuses the rewrite engine's candidate loader, so the two
    versions execute in the module's real namespace with args deep-copied per
    call (mutation can't leak between old and new)."""
    try:
        old_fn = _load_candidate(repo, qualname, old_src)
    except Exception as exc:
        return Verdict(qualname, UNVERIFIED, f"old version failed to load: {exc}")
    try:
        new_fn = _load_candidate(repo, qualname, new_src)
    except Exception as exc:
        return Verdict(qualname, UNVERIFIED, f"new version failed to load: {exc}")
    if not arg_tuples:
        return Verdict(qualname, UNVERIFIED, "no recorded inputs (not exercised by the test suite)")

    for args in arg_tuples:
        old_exc = new_exc = None
        old_out = new_out = None
        try:
            old_out = old_fn(*args)
        except Exception as exc:
            old_exc = exc
        try:
            new_out = new_fn(*args)
        except Exception as exc:
            new_exc = exc
        if old_exc or new_exc:
            # both raising the same exception type is preserved behavior
            if old_exc and new_exc and type(old_exc) is type(new_exc):
                continue
            if old_exc and new_exc:
                return Verdict(
                    qualname,
                    DIVERGED,
                    f"on {args!r}: old raised {type(old_exc).__name__}, "
                    f"new raised {type(new_exc).__name__}",
                )
            raised, ret = (old_exc, new_out) if old_exc else (new_exc, old_out)
            which = "old" if old_exc else "new"
            other = "new" if old_exc else "old"
            return Verdict(
                qualname,
                DIVERGED,
                f"on {args!r}: {which} raised {type(raised).__name__}, {other} returned {ret!r}",
            )
        if not _eq(old_out, new_out):
            return Verdict(
                qualname,
                DIVERGED,
                f"on {args!r}: old returned {old_out!r}, new returned {new_out!r}",
            )
    return Verdict(
        qualname, PRESERVING, f"agreed on {len(arg_tuples)} input(s)", checked=len(arg_tuples)
    )


# --- fuzzing around the recorded inputs (dev-need: tests *and* fuzz) ---------

_INT_MUTATIONS = (0, 1, -1, 2, -2, 255, 256, -256, 2**31 - 1, -(2**31), 2**63 - 1)


def fuzz_around(arg_tuples: list[tuple[Any, ...]], rounds: int) -> list[tuple[Any, ...]]:
    """Deterministic mutations of recorded inputs — swap one scalar arg for an
    edge value at a time. Catches divergences on inputs near, but not exactly,
    what the tests hit (off-by-one, overflow, empty string) without needing a
    type-aware generator. Deterministic (no RNG) so a verdict is reproducible."""
    extra: list[tuple[Any, ...]] = []
    seen = {repr(t) for t in arg_tuples}
    for args in arg_tuples:
        for i, a in enumerate(args):
            muts: tuple[Any, ...]
            if isinstance(a, bool):
                muts = (not a,)
            elif isinstance(a, int):
                muts = _INT_MUTATIONS
            elif isinstance(a, str):
                muts = ("", a + a, a[:-1] if a else "x")
            elif isinstance(a, (bytes, bytearray)):
                muts = (b"", bytes(a) + bytes(a))
            else:
                continue
            for m in muts[:rounds]:
                cand = (*args[:i], m, *args[i + 1 :])
                if repr(cand) not in seen:
                    seen.add(repr(cand))
                    extra.append(cand)
    return extra


# --- the git + ast layer: what changed --------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _module_name(rel_path: str) -> str:
    p = rel_path[:-3] if rel_path.endswith(".py") else rel_path
    p = p[:-9] if p.endswith("/__init__") else p
    return p.replace("/", ".")


def _func_defs(source: str, module: str) -> dict[str, tuple[str, str]]:
    """qualname -> (bare_name, source_text) for top-level functions and one-level
    class methods. Nested/inner functions are out of scope (no stable qualname)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    out: dict[str, tuple[str, str]] = {}

    def add(node: ast.AST, prefix: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[f"{prefix}{node.name}"] = (node.name, ast.get_source_segment(source, node) or "")

    for node in tree.body:
        add(node, f"{module}.")
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                add(sub, f"{module}.{node.name}.")
    return out


def changed_py_functions(repo: Path, base_ref: str) -> tuple[list[ChangedFunction], list[str]]:
    """Functions whose source text differs between ``base_ref`` and the working
    tree. Returns (changed, notes). Deleted files and pure additions are skipped
    (nothing to compare against / no base)."""
    changed: list[ChangedFunction] = []
    notes: list[str] = []
    try:
        names = _git(repo, "diff", "--name-only", base_ref, "--", "*.py").split()
    except subprocess.CalledProcessError as exc:
        return [], [f"git diff failed (is {base_ref!r} a valid ref?): {exc.stderr.strip()}"]
    for rel in names:
        new_file = repo / rel
        if not new_file.exists():
            continue  # deleted
        try:
            old_source = _git(repo, "show", f"{base_ref}:{rel}")
        except subprocess.CalledProcessError:
            continue  # newly added at working tree — no base version
        module = _module_name(rel)
        old_defs = _func_defs(old_source, module)
        new_defs = _func_defs(new_file.read_text(errors="replace"), module)
        for qn, (name, new_src) in new_defs.items():
            if qn in old_defs and old_defs[qn][1] != new_src:
                changed.append(ChangedFunction(qn, rel, name, old_defs[qn][1], new_src))
    return changed, notes


# --- orchestration ----------------------------------------------------------


@dataclass
class DiffReport:
    verdicts: list[Verdict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        s = {PRESERVING: 0, DIVERGED: 0, UNVERIFIED: 0}
        for v in self.verdicts:
            s[v.status] += 1
        return s

    @property
    def ok(self) -> bool:
        """True iff nothing diverged — the CI merge-gate condition."""
        return self.summary[DIVERGED] == 0


def verify_diff(
    repo: Path,
    base_ref: str = "HEAD",
    *,
    fuzz_rounds: int = 0,
    capture_argv: list[str] | None = None,
) -> DiffReport:
    """End to end: find changed functions, record their real inputs by running
    the repo's tests, and differentially replay old vs new (optionally fuzzed).
    Requires ``pytest`` (or ``capture_argv``) to record inputs."""
    from cgir.replay import capture

    changed, notes = changed_py_functions(repo, base_ref)
    report = DiffReport(notes=notes)
    if not changed:
        report.notes.append(f"no changed Python functions vs {base_ref}")
        return report

    targets = {cf.qualname: (Path(cf.path), cf.func_name) for cf in changed}
    try:
        traces: dict[str, list[Trace]] = capture(repo, targets, driver_argv=capture_argv)
    except Exception as exc:
        report.notes.append(f"trace capture failed (need a passing test suite): {exc}")
        traces = {}

    for cf in changed:
        recorded = [args for args, _ in traces.get(cf.qualname, [])]
        args_all = list(recorded)
        n_fuzz = 0
        if fuzz_rounds and recorded:
            fuzzed = fuzz_around(recorded, fuzz_rounds)
            args_all += fuzzed
            n_fuzz = len(fuzzed)
        v = differential_replay(repo, cf.qualname, cf.old_src, cf.new_src, args_all)
        v.fuzzed = n_fuzz if v.status != UNVERIFIED else 0
        report.verdicts.append(v)
    return report
