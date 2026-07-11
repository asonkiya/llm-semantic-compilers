"""The git pre-commit seatbelt.

A local, deterministic gate on your own — and your agent's — commits.
On commit, ``run_check`` scans the last committed tree (``HEAD``) against
the *staged* tree (``git write-tree``), diffs the contracts, and reports:

* **violations** — the ``--fail-on`` drift rules that trip (defaults to the
  evidence-based low-noise set from ``docs/gate-noise.md``);
* **tests to run** — the union of :func:`compute_typed_impact` over every
  changed component, narrowed by *its* contract delta, so a body-only
  refactor asks for nothing downstream.

Design choices that matter for a hook:

* **Scans the staged tree, not the working tree** — it checks exactly what
  will be committed (``git write-tree`` materialises the index as a tree).
* **Fail-open on internal error** — a bug in CGIR must never block your
  commit; only a real contract violation does.
* **Skips non-source commits** — no scan when nothing with a supported
  extension is staged.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from cgir.ir.component_spec import ComponentSpec
from cgir.languages import ADAPTERS
from cgir.pipeline import scan_repo
from cgir.report.diff import compute_diff, render_diff, violations
from cgir.report.impact import compute_typed_impact
from cgir.report.pins import change_violations, state_violations

# The low-noise default from the real-history noise measurement: fail only
# when something starts — or silently stops — reaching the outside world.
DEFAULT_FAIL_ON: tuple[str, ...] = (
    "effect-gain:net",
    "effect-gain:fs",
    "effect-gain:db",
    "effect-loss:net",
    "effect-loss:fs",
    "effect-loss:db",
)

_SUPPORTED_EXTS: frozenset[str] = frozenset(
    ext for adapter in ADAPTERS.values() for ext in adapter.file_extensions
)

_HOOK_TEMPLATE = """\
#!/bin/sh
# CGIR contract seatbelt — installed by `cgir hook install`. Delete to remove.
if ! command -v cgir >/dev/null 2>&1; then
  echo "cgir not on PATH; skipping contract check" >&2
  exit 0
fi
exec cgir hook run {flags}
"""


@dataclass
class HookResult:
    checked: bool
    violations: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    diff_text: str = ""
    error: str | None = None


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _staged_supported(repo: Path) -> bool:
    names = _git(repo, "diff", "--cached", "--name-only").splitlines()
    return any(Path(n).suffix in _SUPPORTED_EXTS for n in names)


def _scan_tree(repo: Path, tree: str) -> tuple[list[ComponentSpec], dict[str, dict[str, str]]]:
    """Scan the content of a git tree-ish out-of-tree: (specs, type shapes)."""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src"
        src.mkdir()
        archive = subprocess.check_output(["git", "-C", str(repo), "archive", tree])
        subprocess.run(["tar", "-x", "-C", str(src)], input=archive, check=True)
        with tempfile.TemporaryDirectory() as od:
            result = scan_repo(src, out=Path(od))
            return result.specs, result.types


def run_check(repo: Path, fail_on: list[str] | None = None) -> HookResult:
    """Contract-check the staged tree against HEAD. Fail-open on any error."""
    rules = list(fail_on) if fail_on is not None else list(DEFAULT_FAIL_ON)
    try:
        if not _staged_supported(repo):
            return HookResult(checked=False)
        staged_tree = _git(repo, "write-tree")
        try:
            head_tree = _git(repo, "rev-parse", "HEAD^{tree}")
        except subprocess.CalledProcessError:
            head_tree = None  # unborn branch — the initial commit
        if head_tree == staged_tree:
            return HookResult(checked=False)

        base_specs, base_types = _scan_tree(repo, head_tree) if head_tree else ([], {})
        head_specs, head_types = _scan_tree(repo, staged_tree)
    except Exception as exc:  # never block a commit because of our own bug
        return HookResult(checked=False, error=f"{type(exc).__name__}: {exc}")

    diff = compute_diff(base_specs, head_specs, old_types=base_types, new_types=head_types)
    found = violations(diff, rules)
    # Pin invariants are always enforced, independent of --fail-on rules.
    found += change_violations(base_specs, head_specs)
    found += state_violations(head_specs)
    changed = [c["id"] for c in diff["changed"]]

    tests: set[str] = set()
    for change in diff["changed"]:
        typed = compute_typed_impact(head_specs, change["id"], list(change["fields"].keys()))
        tests |= set(typed["tests"])

    return HookResult(
        checked=True,
        violations=found,
        changed=changed,
        tests=sorted(tests),
        diff_text=render_diff(diff),
    )


def render_hook(result: HookResult) -> str:
    if result.error:
        return f"cgir: skipped contract check ({result.error})\n"
    if not result.checked:
        return ""
    lines = ["cgir contract seatbelt"]
    if result.changed:
        lines.append(f"  contract changes in {len(result.changed)} component(s)")
    if result.tests:
        lines.append(f"  tests to run ({len(result.tests)}):")
        lines.extend(f"    • {t}" for t in result.tests)
    if result.violations:
        lines.append("")
        lines.append(f"  ❌ blocked — {len(result.violations)} drift violation(s):")
        lines.extend(f"    ! {v}" for v in result.violations)
        lines.append("  (bypass once with `git commit --no-verify`)")
    else:
        lines.append("  ✅ no blocking contract drift")
    return "\n".join(lines) + "\n"


def _hooks_dir(repo: Path) -> Path:
    return Path(_git(repo, "rev-parse", "--git-path", "hooks"))


def install(repo: Path, fail_on: list[str] | None = None, force: bool = False) -> Path:
    """Write ``.git/hooks/pre-commit`` invoking ``cgir hook run``."""
    rules = list(fail_on) if fail_on is not None else list(DEFAULT_FAIL_ON)
    hooks = _hooks_dir(repo)
    if not hooks.is_absolute():
        hooks = repo / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    path = hooks / "pre-commit"
    if path.exists() and not force:
        raise FileExistsError(f"{path} exists; pass force=True to overwrite")
    flags = " ".join(f"--fail-on {r}" for r in rules)
    path.write_text(_HOOK_TEMPLATE.format(flags=flags))
    path.chmod(0o755)
    return path


def uninstall(repo: Path) -> bool:
    """Remove the hook if it is ours. Returns whether anything was removed."""
    hooks = _hooks_dir(repo)
    if not hooks.is_absolute():
        hooks = repo / hooks
    path = hooks / "pre-commit"
    if path.exists() and "cgir hook run" in path.read_text():
        path.unlink()
        return True
    return False


__all__ = [
    "DEFAULT_FAIL_ON",
    "HookResult",
    "install",
    "render_hook",
    "run_check",
    "uninstall",
]
