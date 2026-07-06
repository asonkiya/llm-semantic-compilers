"""cgir lint — semantic architecture rules over the ComponentSpec index.

Import linters (Tach, import-linter) constrain *imports*. CGIR constrains
*meaning*: which components may carry which effects, what kind they must be,
and what they may call — checks an import graph can't express because it
doesn't know a function touches the network or routes to the DB layer.

A rule is scoped by an ``in`` id-glob and carries one predicate:

* ``forbid-effect``: matched components must not carry these effect tags
* ``require-kind``: matched components must be this component kind
* ``forbid-call``: matched components must not call components whose id
  matches this glob (a semantic layer boundary over resolved CALLS)

Rules live in ``cgir.toml`` as a ``[[rule]]`` array; the checker itself is
pure over specs so it is trivially testable and CI-friendly.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from cgir.ir.component_spec import ComponentSpec


@dataclass(slots=True)
class LintViolation:
    rule: str
    component: str
    detail: str


def load_rules(config_path: Path) -> list[dict[str, Any]]:
    """Read ``[[rule]]`` entries from a ``cgir.toml``-style config."""
    data = tomllib.loads(config_path.read_text())
    rules = data.get("rule", [])
    return list(rules) if isinstance(rules, list) else []


def lint(specs: list[ComponentSpec], rules: list[dict[str, Any]]) -> list[LintViolation]:
    violations: list[LintViolation] = []
    known = {s.id for s in specs}
    for rule in rules:
        name = str(rule.get("name", "rule"))
        scope = str(rule.get("in", "*"))
        matched = [s for s in specs if fnmatch(s.id, scope)]

        if "forbid-effect" in rule:
            forbidden = set(rule["forbid-effect"])
            for spec in matched:
                hit = sorted(forbidden & set(spec.effects))
                if hit:
                    violations.append(
                        LintViolation(name, spec.id, f"has forbidden effect(s): {', '.join(hit)}")
                    )
        if "require-kind" in rule:
            want = str(rule["require-kind"])
            for spec in matched:
                if spec.kind.value != want:
                    violations.append(
                        LintViolation(name, spec.id, f"is {spec.kind.value}, must be {want}")
                    )
        if "forbid-call" in rule:
            target_glob = str(rule["forbid-call"])
            for spec in matched:
                for callee in spec.calls:
                    if callee in known and fnmatch(callee, target_glob):
                        violations.append(
                            LintViolation(name, spec.id, f"calls forbidden target: {callee}")
                        )
    return violations


def render_lint(violations: list[LintViolation]) -> str:
    if not violations:
        return "no architecture-rule violations\n"
    lines = [f"{len(violations)} architecture-rule violation(s):"]
    for v in violations:
        lines.append(f"  ! [{v.rule}] {v.component} {v.detail}")
    return "\n".join(lines) + "\n"
