"""Program dependence graph overlay.

Adds two edge kinds onto the existing CFG-enriched graph:

* ``FLOWS_TO`` — data dependence. For each definition D (a ``Parameter``
  or any CFG node with a non-empty ``writes`` attr — ``Assignment``,
  ``for`` header, ``with`` header, ``except`` clause) writing variable v,
  and each CFG node N that *reads* v, emit ``D -[FLOWS_TO]-> N`` iff D
  reaches N (per :mod:`cgir.analyses.reaching_defs`).
* ``DEPENDS_ON`` — control dependence. For each CFG node N with
  ``attrs["controlled_by"]`` set, emit ``N -[DEPENDS_ON]-> controller``.
  The controller is the immediately enclosing ``Branch`` or ``Loop``, as
  recorded by :mod:`cgir.analyses.cfg`.

Pure-graph pass: no ``repo_path``, no re-parse. Idempotency is *not*
guaranteed — calling ``build`` twice will duplicate edges. The CLI calls
it once.
"""

from __future__ import annotations

from cgir.analyses import reaching_defs
from cgir.ir.edges import Edge, EdgeKind
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


def build(graph: RepoGraph) -> None:
    rd = reaching_defs.compute(graph)
    for func in list(graph.nodes()):
        if func.kind in {NodeKind.Function, NodeKind.Method}:
            _build_for_function(graph, func, rd)


def _build_for_function(graph: RepoGraph, func: Node, rd: dict[str, set[str]]) -> None:
    cfg_nodes = [c for c in graph.children(func.id) if c.kind in _CFG_KINDS]
    if not cfg_nodes:
        return
    params = list(graph.children(func.id, NodeKind.Parameter))

    # var → {def_id, ...} (Assignments + Parameters)
    var_to_defs: dict[str, set[str]] = {}
    for p in params:
        var_to_defs.setdefault(p.name, set()).add(p.id)
    for n in cfg_nodes:
        writes = n.attrs.get("writes") if n.attrs else None
        if isinstance(writes, list):
            for v in writes:
                var_to_defs.setdefault(v, set()).add(n.id)

    # FLOWS_TO: def -> use, gated by reaching defs.
    for n in cfg_nodes:
        reads = n.attrs.get("reads") if n.attrs else None
        if not isinstance(reads, list) or not reads:
            continue
        in_defs = rd.get(n.id, set())
        for var in reads:
            for def_id in var_to_defs.get(var, set()) & in_defs:
                graph.add_edge(Edge(src=def_id, dst=n.id, kind=EdgeKind.FLOWS_TO))

    # DEPENDS_ON: controlled node -> controlling Branch/Loop.
    for n in cfg_nodes:
        controller = n.attrs.get("controlled_by") if n.attrs else None
        if isinstance(controller, str):
            graph.add_edge(Edge(src=n.id, dst=controller, kind=EdgeKind.DEPENDS_ON))
