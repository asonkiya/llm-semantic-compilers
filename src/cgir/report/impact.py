"""Change-impact / blast-radius analysis — the forward-looking companion
to :mod:`cgir.report.diff`.

``diff`` is retrospective ("what changed?"). ``impact`` is predictive:
*before* you (or an agent) touch a component, what does changing it put at
risk? The answer is the transitive **upstream** (caller) closure — every
component whose behavior depends on this one — plus the surface that closure
reaches:

* which **entrypoints** (HTTP routes / CLI commands / tasks) sit above it,
  i.e. how the outside world can observe a break;
* which **tests** to run — the union of ``covered_by`` over the target and
  everything affected — deterministic test selection with no guessing.

Pure over ComponentSpecs (same pattern as :mod:`cgir.report.stats` and
:mod:`cgir.report.diff`), so it is trivially testable and drives both the
CLI (``cgir impact``) and the MCP tool an agent calls before editing.
"""

from __future__ import annotations

from typing import Any

from cgir.ir.component_spec import ComponentSpec


def compute_impact(specs: list[ComponentSpec], target_id: str) -> dict[str, Any]:
    """Blast radius of changing ``target_id`` — pure, JSON-able."""
    by_id = {s.id: s for s in specs}
    if target_id not in by_id:
        raise KeyError(target_id)

    # Reverse the CALLS relation: callee -> the components that call it.
    callers: dict[str, set[str]] = {}
    for spec in specs:
        for callee in spec.calls:
            if callee in by_id:
                callers.setdefault(callee, set()).add(spec.id)

    direct = sorted(callers.get(target_id, set()))

    # Transitive upstream closure (BFS), cycle-safe, target excluded.
    affected: set[str] = set()
    queue = [target_id]
    while queue:
        node = queue.pop()
        for caller in callers.get(node, ()):
            if caller != target_id and caller not in affected:
                affected.add(caller)
                queue.append(caller)

    surface = sorted(affected | {target_id})
    entrypoints = [
        {"id": sid, "entrypoint": by_id[sid].entrypoint} for sid in surface if by_id[sid].entrypoint
    ]

    tests: set[str] = set(by_id[target_id].covered_by)
    for sid in affected:
        tests |= set(by_id[sid].covered_by)

    return {
        "target": target_id,
        "direct_callers": direct,
        "affected": sorted(affected),
        "entrypoints": entrypoints,
        "tests": sorted(tests),
    }


def render_impact(specs: list[ComponentSpec], target_id: str) -> str:
    """Human summary of the blast radius."""
    imp = compute_impact(specs, target_id)
    affected = imp["affected"]
    entrypoints = imp["entrypoints"]
    tests = imp["tests"]

    lines: list[str] = [f"# impact of changing {target_id}", ""]
    lines.append(
        f"{len(affected)} component(s) affected · "
        f"{len(entrypoints)} entrypoint(s) at risk · "
        f"{len(tests)} test(s) to run"
    )

    lines.append("")
    lines.append(f"affected components ({len(affected)}):")
    if affected:
        direct = set(imp["direct_callers"])
        for sid in affected:
            lines.append(f"  {'← direct' if sid in direct else '  ⋯    '}  {sid}")
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


__all__ = ["compute_impact", "render_impact"]
