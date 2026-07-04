"""Mermaid call-graph rendering from ComponentSpecs.

Produces ``flowchart LR`` text suitable for embedding in Markdown (GitHub,
Obsidian, mermaid.live). One node per component, one ``subgraph`` per
source file, one edge per resolved intra-repo call; nodes are colored by
:class:`~cgir.ir.component_spec.ComponentKind`.

Calls to components outside the spec set (stdlib, third-party) are skipped
rather than rendered as dangling nodes — the diagram shows *this* repo.
"""

from __future__ import annotations

import re

from cgir.ir.component_spec import ComponentSpec

_KIND_STYLES: dict[str, str] = {
    "pure_function": "fill:#c6f6d5,stroke:#2f855a,color:#1a202c",
    "orchestrator": "fill:#bee3f8,stroke:#2b6cb0,color:#1a202c",
    "state_transformer": "fill:#feebc8,stroke:#c05621,color:#1a202c",
    "effect_adapter": "fill:#fed7d7,stroke:#c53030,color:#1a202c",
    "unknown": "fill:#e2e8f0,stroke:#4a5568,color:#1a202c",
}


def render_call_graph(specs: list[ComponentSpec]) -> str:
    """Render the component call graph as Mermaid flowchart text."""
    lines = ["flowchart LR"]
    known_ids = {s.id for s in specs}

    by_file: dict[str, list[ComponentSpec]] = {}
    for spec in specs:
        file = spec.trace[0].rsplit(":", 1)[0] if spec.trace else "(unknown)"
        by_file.setdefault(file, []).append(spec)

    for file, group in sorted(by_file.items()):
        lines.append(f'    subgraph {_mermaid_id(file)}["{file}"]')
        for spec in group:
            lines.append(f'        {_mermaid_id(spec.id)}["{spec.id}"]')
        lines.append("    end")

    for spec in specs:
        for callee in spec.calls:
            if callee in known_ids:
                lines.append(f"    {_mermaid_id(spec.id)} --> {_mermaid_id(callee)}")

    for kind in sorted({s.kind.value for s in specs}):
        lines.append(f"    classDef {kind} {_KIND_STYLES[kind]}")
    for spec in specs:
        lines.append(f"    class {_mermaid_id(spec.id)} {spec.kind.value}")

    return "\n".join(lines) + "\n"


def _mermaid_id(raw: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]", "_", raw)
