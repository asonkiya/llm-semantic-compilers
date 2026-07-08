"""Change-impact / blast-radius analysis — the forward-looking companion
to :mod:`cgir.report.diff`.

``diff`` is retrospective ("what changed?"). ``impact`` is predictive:
*before* you (or an agent) touch a component, what does changing it put at
risk? The answer is the transitive **upstream** (caller) closure — every
component whose behaviour depends on this one — plus the surface that
closure reaches: which **entrypoints** sit above it, and which **tests** to
run (union of ``covered_by`` over the target and everything affected).

``compute_impact`` is the worst case: it assumes the change could affect
anything upstream. ``compute_typed_impact`` narrows that by *what actually
changed about the contract*, because not every change propagates the same
way:

* **body-only** (no contract field changed) — callers are contract-safe;
  reach ``none``, only the target's own tests matter.
* **signature / outputs** — an interface break the *direct* call sites must
  adapt to, but which does not inherently ripple past them; reach
  ``direct``.
* **effects / purity / kind** — semantic taint that flows *up* the call
  graph (a caller of something newly effectful is itself newly effectful);
  reach ``transitive`` — the full closure.

The reach model is a deliberate, documented heuristic, not a proof: it
mirrors how the effect/purity analyses actually propagate. Pure over
ComponentSpecs, so it drives both the CLI (``cgir impact``) and the MCP
tool an agent calls before — and after — an edit.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from cgir.ir.component_spec import ComponentSpec

# Contract fields whose change taints callers transitively vs. only directly.
_TRANSITIVE_FIELDS = frozenset({"effects", "purity", "kind"})
_DIRECT_FIELDS = frozenset({"signature", "outputs", "inputs"})


def _callers_map(
    specs: list[ComponentSpec], by_id: dict[str, ComponentSpec]
) -> dict[str, set[str]]:
    """Reverse the CALLS relation: callee id -> ids that call it."""
    callers: dict[str, set[str]] = {}
    for spec in specs:
        for callee in spec.calls:
            if callee in by_id:
                callers.setdefault(callee, set()).add(spec.id)
    return callers


def _upstream_closure(callers: dict[str, set[str]], target_id: str) -> set[str]:
    """Transitive caller closure of ``target_id`` (cycle-safe, target excluded)."""
    affected: set[str] = set()
    queue = [target_id]
    while queue:
        node = queue.pop()
        for caller in callers.get(node, ()):
            if caller != target_id and caller not in affected:
                affected.add(caller)
                queue.append(caller)
    return affected


def _surface(
    by_id: dict[str, ComponentSpec], target_id: str, affected: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Entrypoints at risk and tests to run over {target} + affected."""
    scope = sorted(affected | {target_id})
    entrypoints = [
        {"id": sid, "entrypoint": by_id[sid].entrypoint} for sid in scope if by_id[sid].entrypoint
    ]
    tests: set[str] = set(by_id[target_id].covered_by)
    for sid in affected:
        tests |= set(by_id[sid].covered_by)
    return entrypoints, sorted(tests)


def compute_impact(specs: list[ComponentSpec], target_id: str) -> dict[str, Any]:
    """Worst-case blast radius of changing ``target_id`` — pure, JSON-able."""
    by_id = {s.id: s for s in specs}
    if target_id not in by_id:
        raise KeyError(target_id)
    callers = _callers_map(specs, by_id)
    affected = _upstream_closure(callers, target_id)
    entrypoints, tests = _surface(by_id, target_id, affected)
    return {
        "target": target_id,
        "direct_callers": sorted(callers.get(target_id, set())),
        "affected": sorted(affected),
        "entrypoints": entrypoints,
        "tests": tests,
    }


def _reach(changed_fields: set[str]) -> str:
    if changed_fields & _TRANSITIVE_FIELDS:
        return "transitive"
    if changed_fields & _DIRECT_FIELDS:
        return "direct"
    return "none"


def compute_typed_impact(
    specs: list[ComponentSpec], target_id: str, changed_fields: Iterable[str]
) -> dict[str, Any]:
    """Blast radius narrowed by *which* contract fields changed.

    ``changed_fields`` is the set of drifted contract fields — e.g. the keys
    of a :func:`cgir.report.diff.compute_diff` change entry or a
    ``VerifyResult.drift``. An empty set means a body-only edit.
    """
    by_id = {s.id: s for s in specs}
    if target_id not in by_id:
        raise KeyError(target_id)
    delta = set(changed_fields)
    reach = _reach(delta)
    callers = _callers_map(specs, by_id)
    direct = sorted(callers.get(target_id, set()))

    if reach == "transitive":
        affected = _upstream_closure(callers, target_id)
    elif reach == "direct":
        affected = set(direct)
    else:
        affected = set()

    entrypoints, tests = _surface(by_id, target_id, affected)
    return {
        "target": target_id,
        "changed_fields": sorted(delta),
        "reach": reach,
        "direct_callers": direct,
        "affected": sorted(affected),
        "entrypoints": entrypoints,
        "tests": tests,
    }


_REACH_NOTE = {
    "none": "body-only change — no contract drift, callers are unaffected",
    "direct": "interface change — direct call sites must adapt",
    "transitive": "effect/purity change — taint flows up the call graph",
}


def _render(data: dict[str, Any]) -> str:
    target = data["target"]
    affected = data["affected"]
    entrypoints = data["entrypoints"]
    tests = data["tests"]
    reach = data.get("reach")

    lines: list[str] = [f"# impact of changing {target}", ""]
    if reach is not None:
        fields = ", ".join(data["changed_fields"]) or "(none)"
        lines.append(f"contract delta: {fields}  →  reach: {reach}")
        lines.append(f"  {_REACH_NOTE[reach]}")
        lines.append("")
    lines.append(
        f"{len(affected)} component(s) affected · "
        f"{len(entrypoints)} entrypoint(s) at risk · "
        f"{len(tests)} test(s) to run"
    )

    lines.append("")
    lines.append(f"affected components ({len(affected)}):")
    if affected:
        direct = set(data["direct_callers"])
        for sid in affected:
            lines.append(f"  {'← direct' if sid in direct else '  ⋯    '}  {sid}")
    elif reach == "none" and data["direct_callers"]:
        lines.append(f"  (none contract-affected; {len(data['direct_callers'])} call it)")
    else:
        lines.append("  (none — nothing calls this)")

    lines.append("")
    lines.append(f"entrypoints at risk ({len(entrypoints)}):")
    if entrypoints:
        for e in entrypoints:
            lines.append(f"  ! {e['entrypoint']}  ({e['id']})")
    else:
        lines.append("  (none reachable)")

    lines.append("")
    lines.append(f"tests to run ({len(tests)}):")
    if tests:
        lines.extend(f"  • {t}" for t in tests)
    else:
        lines.append("  (no linked tests — this change is unguarded)")

    return "\n".join(lines) + "\n"


def render_impact(specs: list[ComponentSpec], target_id: str) -> str:
    """Human summary of the worst-case blast radius."""
    return _render(compute_impact(specs, target_id))


def render_typed_impact(
    specs: list[ComponentSpec], target_id: str, changed_fields: Iterable[str]
) -> str:
    """Human summary of the blast radius narrowed by the contract delta."""
    return _render(compute_typed_impact(specs, target_id, changed_fields))


__all__ = [
    "compute_impact",
    "compute_typed_impact",
    "render_impact",
    "render_typed_impact",
]
