"""Component flow tracing — who calls it, what it calls, what it builds.

``render_flow`` is pure over ComponentSpecs (same pattern as
:mod:`cgir.report.stats`): upstream callers and downstream callees as
depth-limited trees, each node annotated with kind, effects, and the
declared return type. Cycles are marked and not re-expanded.
"""

from __future__ import annotations

from collections.abc import Callable

from cgir.ir.component_spec import ComponentSpec

DEFAULT_DEPTH = 3


def render_flow(specs: list[ComponentSpec], root_id: str, depth: int = DEFAULT_DEPTH) -> str:
    by_id = {s.id: s for s in specs}
    if root_id not in by_id:
        raise KeyError(root_id)

    callers: dict[str, list[str]] = {}
    for spec in specs:
        for callee in spec.calls:
            if callee in by_id:
                callers.setdefault(callee, []).append(spec.id)

    def upstream(node_id: str) -> list[str]:
        return sorted(callers.get(node_id, []))

    def downstream(node_id: str) -> list[str]:
        return [c for c in by_id[node_id].calls if c in by_id]

    lines: list[str] = [_describe(by_id[root_id]), ""]
    lines.append("called by (upstream):")
    _walk(root_id, upstream, by_id, lines, "<-", depth, indent=1, seen={root_id})
    lines.append("")
    lines.append("calls (downstream):")
    _walk(root_id, downstream, by_id, lines, "->", depth, indent=1, seen={root_id})
    root_constructs = by_id[root_id].constructs
    if root_constructs:
        lines.append("")
        lines.append("constructs:")
        for type_name in root_constructs:
            lines.append(f"  + {type_name}")
    return "\n".join(lines) + "\n"


def _walk(
    node_id: str,
    next_ids: Callable[[str], list[str]],
    by_id: dict[str, ComponentSpec],
    lines: list[str],
    arrow: str,
    depth: int,
    indent: int,
    seen: set[str],
) -> None:
    children = next_ids(node_id)
    if not children and indent == 1:
        lines.append("  (none)")
        return
    for child in children:
        pad = "  " * indent
        if child in seen:
            lines.append(f"{pad}{arrow} {child}  (cycle)")
            continue
        lines.append(f"{pad}{arrow} {_describe(by_id[child])}")
        if indent < depth:
            _walk(child, next_ids, by_id, lines, arrow, depth, indent + 1, seen | {child})


def _describe(spec: ComponentSpec) -> str:
    parts = [spec.id, f"[{spec.kind.value}"]
    effects = [e for e in spec.effects if e != "calls_effectful"]
    if effects:
        parts[-1] += " · " + ",".join(effects)
    parts[-1] += "]"
    if spec.outputs:
        parts.append(f"-> {spec.outputs[0]}")
    if spec.entrypoint:
        parts.append(f"({spec.entrypoint})")
    return "  ".join(parts)
