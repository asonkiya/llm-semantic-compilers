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
        if rule.get("forbid-cycle"):
            violations.extend(_cycle_violations(name, matched))
        if "layers" in rule:
            layer_globs = [str(g) for g in rule["layers"]]
            violations.extend(_layer_violations(name, specs, layer_globs))
    return violations


def _cycle_violations(name: str, matched: list[ComponentSpec]) -> list[LintViolation]:
    """Call-graph cycles among the matched components (Tarjan SCCs).

    Self-recursion (a component calling itself) is normal, not a cycle —
    only strongly connected components of size >=2 fire.
    """
    in_scope = {s.id for s in matched}
    adj = {s.id: [c for c in s.calls if c in in_scope] for s in matched}

    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = [0]
    sccs: list[list[str]] = []

    def strongconnect(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in adj.get(v, []):
            if w not in index:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            component: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            if len(component) > 1:
                sccs.append(sorted(component))

    for node in sorted(adj):
        if node not in index:
            strongconnect(node)

    return [
        LintViolation(name, scc[0], f"call cycle: {' -> '.join(scc)} -> {scc[0]}")
        for scc in sorted(sccs)
    ]


def _layer_violations(
    name: str, specs: list[ComponentSpec], layer_globs: list[str]
) -> list[LintViolation]:
    """Dependencies must point downward through the ordered layers.

    A component in a lower layer (higher index) calling one in a strictly
    higher layer is a violation; same-layer and downward (including
    layer-skipping) calls are fine. Components matching no layer are
    ignored — layers constrain only what they name.
    """

    def layer_of(spec_id: str) -> int | None:
        for i, glob in enumerate(layer_globs):
            if fnmatch(spec_id, glob):
                return i
        return None

    layer_index = {s.id: layer_of(s.id) for s in specs}
    violations: list[LintViolation] = []
    for spec in specs:
        src_layer = layer_index.get(spec.id)
        if src_layer is None:
            continue
        for callee in spec.calls:
            dst_layer = layer_index.get(callee)
            if dst_layer is not None and dst_layer < src_layer:
                violations.append(
                    LintViolation(
                        name,
                        spec.id,
                        f"layer {layer_globs[src_layer]!r} calls up into "
                        f"{layer_globs[dst_layer]!r}: {callee}",
                    )
                )
    return violations


def render_lint(violations: list[LintViolation]) -> str:
    if not violations:
        return "no architecture-rule violations\n"
    lines = [f"{len(violations)} architecture-rule violation(s):"]
    for v in violations:
        lines.append(f"  ! [{v.rule}] {v.component} {v.detail}")
    return "\n".join(lines) + "\n"
