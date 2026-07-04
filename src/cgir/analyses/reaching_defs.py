"""Reaching-definitions analysis.

Classical forward may-analysis over each function's CFG:

* Any CFG node A with a non-empty ``writes`` attr is a definition of those
  variables: ``gen[A] = {A.id}`` and ``kill[A]`` contains every other def
  of any variable A writes. This covers ``Assignment`` nodes, ``for``-loop
  headers (loop targets), ``with`` headers (``as`` aliases), and ``except``
  clauses (``as`` aliases).
* ``Parameter`` nodes count as initial defs at function entry.
* ``in[n] = union of out[pred]`` over ``CONTROLS`` predecessors within
  the same function; for the entry node(s), ``in`` also contains the
  parameter defs.
* ``out[n] = gen[n] | (in[n] - kill[n])``.

This is the first pure-graph analysis: it reads the ``writes`` attrs
populated by :mod:`cgir.analyses.cfg` and walks the graph. It does not
re-parse source. See ``docs/roadmap.md`` "Grammar-agnostic core refactor".
"""

from __future__ import annotations

from collections import deque

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


def compute(graph: RepoGraph) -> dict[str, set[str]]:
    """Return ``{cfg_node_id: {def_id, ...}}`` across every function/method.

    ``def_id`` is the id of a ``Parameter`` node or any CFG node with a
    non-empty ``writes`` attr.
    """
    result: dict[str, set[str]] = {}
    for func in list(graph.nodes()):
        if func.kind in {NodeKind.Function, NodeKind.Method}:
            result.update(_compute_for_function(graph, func))
    return result


def _compute_for_function(graph: RepoGraph, func: Node) -> dict[str, set[str]]:
    cfg_nodes = [c for c in graph.children(func.id) if c.kind in _CFG_KINDS]
    if not cfg_nodes:
        return {}
    params = list(graph.children(func.id, NodeKind.Parameter))

    cfg_ids: set[str] = {n.id for n in cfg_nodes}

    # var -> {def_id, ...} (Assignment and Parameter)
    defs_by_var: dict[str, set[str]] = {}
    for p in params:
        defs_by_var.setdefault(p.name, set()).add(p.id)
    writes_per_node: dict[str, list[str]] = {}
    for n in cfg_nodes:
        writes_raw = n.attrs.get("writes") if n.attrs else None
        writes = list(writes_raw) if isinstance(writes_raw, list) else []
        if writes:
            writes_per_node[n.id] = writes
        for var in writes:
            defs_by_var.setdefault(var, set()).add(n.id)

    gen: dict[str, set[str]] = {}
    kill: dict[str, set[str]] = {}
    for n in cfg_nodes:
        if n.id in writes_per_node:
            gen[n.id] = {n.id}
            kill_set: set[str] = set()
            for var in writes_per_node[n.id]:
                kill_set |= defs_by_var.get(var, set()) - {n.id}
            kill[n.id] = kill_set
        else:
            gen[n.id] = set()
            kill[n.id] = set()

    entry_defs: set[str] = {p.id for p in params}

    in_set: dict[str, set[str]] = {nid: set() for nid in cfg_ids}
    out_set: dict[str, set[str]] = {nid: set() for nid in cfg_ids}

    worklist: deque[str] = deque(cfg_ids)
    queued: set[str] = set(cfg_ids)

    while worklist:
        nid = worklist.popleft()
        queued.discard(nid)

        new_in: set[str] = set()
        for pred_edge in graph.in_edges(nid, EdgeKind.CONTROLS):
            if pred_edge.src == func.id:
                new_in |= entry_defs
            elif pred_edge.src in cfg_ids:
                new_in |= out_set[pred_edge.src]
        in_set[nid] = new_in

        new_out = gen[nid] | (new_in - kill[nid])
        if new_out != out_set[nid]:
            out_set[nid] = new_out
            for succ_edge in graph.out_edges(nid, EdgeKind.CONTROLS):
                if succ_edge.dst in cfg_ids and succ_edge.dst not in queued:
                    worklist.append(succ_edge.dst)
                    queued.add(succ_edge.dst)

    return {nid: in_set[nid] for nid in cfg_ids}
