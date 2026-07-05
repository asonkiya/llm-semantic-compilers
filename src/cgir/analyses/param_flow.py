"""Parameter-flow analysis: which caller params reach which callee.

Pure-graph may-analysis over the CFG ``reads``/``writes`` attrs populated
by :mod:`cgir.analyses.cfg` and the call-site ``args`` recorded on
``CALLS`` edges by :mod:`cgir.analyses.call_graph`:

* Each parameter taints its own name.
* Any CFG node that reads a tainted name taints every name it writes
  (fixed point, order-insensitive — a *may*-flow, per spec: flag rather
  than prove).
* A parameter flows into a call iff the call's argument identifiers
  intersect the parameter's tainted names.

Drives the arg-flow edges in the HTML Flow view (data passed *down* into
callees, complementing the return-type edges that flow up).
"""

from __future__ import annotations

from typing import Any

from cgir.ir.edges import EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import Node, NodeKind

_CFG_KINDS: frozenset[NodeKind] = frozenset(
    {
        NodeKind.Statement,
        NodeKind.Assignment,
        NodeKind.Branch,
        NodeKind.Loop,
        NodeKind.Return,
    }
)


def compute(graph: RepoGraph) -> dict[str, list[dict[str, Any]]]:
    """Per caller id: ``[{"callee": id, "params": [names...]}, ...]``."""
    result: dict[str, list[dict[str, Any]]] = {}
    for func in graph.nodes():
        if func.kind not in {NodeKind.Function, NodeKind.Method}:
            continue
        entries = _entries_for(graph, func)
        if entries:
            result[func.id] = entries
    return result


def _entries_for(graph: RepoGraph, func: Node) -> list[dict[str, Any]]:
    params = [p.name for p in graph.children(func.id, NodeKind.Parameter)]
    if not params:
        return []
    cfg_nodes = [c for c in graph.children(func.id) if c.kind in _CFG_KINDS]

    taint: dict[str, set[str]] = {p: {p} for p in params}
    changed = True
    while changed:
        changed = False
        for node in cfg_nodes:
            reads = set(node.attrs.get("reads") or []) if node.attrs else set()
            writes = set(node.attrs.get("writes") or []) if node.attrs else set()
            if not writes:
                continue
            for param in params:
                if reads & taint[param] and not writes <= taint[param]:
                    taint[param] |= writes
                    changed = True

    entries: list[dict[str, Any]] = []
    for edge in graph.out_edges(func.id, EdgeKind.CALLS):
        args = set(edge.attrs.get("args") or [])
        flowing = [p for p in params if args & taint[p]]
        if flowing:
            entries.append({"callee": edge.dst, "params": flowing})
    return entries
