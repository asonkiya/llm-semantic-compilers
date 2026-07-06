"""Slice each Function/Method node into a ComponentSpec."""

from __future__ import annotations

from cgir.analyses.effects import IMPURE_EFFECT_TAGS, TRANSITIVE_TAG
from cgir.analyses.entrypoints import detect as detect_entrypoint
from cgir.analyses.purity import PLACEHOLDER_SCORE
from cgir.ir.component_spec import ComponentKind, ComponentSpec
from cgir.ir.edges import EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import NodeKind


def slice_components(
    graph: RepoGraph,
    effects: dict[str, list[str]] | None = None,
    purity_scores: dict[str, float] | None = None,
    language: str = "python",
) -> list[ComponentSpec]:
    effects = effects or {}
    purity_scores = purity_scores or {}
    specs: list[ComponentSpec] = []

    for node in graph.nodes():
        if node.kind not in {NodeKind.Function, NodeKind.Method}:
            continue

        qual = str(node.attrs.get("qualname") or node.name)
        inputs = [param.name for param in graph.children(node.id, NodeKind.Parameter)]
        calls, constructs = _split_callees(graph, node.id)
        node_effects = effects.get(node.id, [])
        purity_score = purity_scores.get(node.id, PLACEHOLDER_SCORE)
        mutates_state = _has_mutations(graph, node.id)
        kind = _classify(node_effects, purity_score, mutates_state)

        trace = []
        if node.path is not None and node.start_line is not None:
            trace.append(f"{node.path}:{node.start_line}")

        attrs = node.attrs or {}
        signature = attrs.get("signature")
        returns = attrs.get("returns")
        decorators = attrs.get("decorators")
        doc = attrs.get("doc")
        raises = attrs.get("raises")

        specs.append(
            ComponentSpec(
                id=qual,
                kind=kind,
                inputs=inputs,
                outputs=[returns] if isinstance(returns, str) and returns else [],
                effects=list(node_effects),
                calls=calls,
                constructs=constructs,
                trace=trace,
                language=language,
                signature=signature if isinstance(signature, str) else None,
                entrypoint=detect_entrypoint(
                    decorators if isinstance(decorators, list) else [], node.name
                ),
                doc=doc if isinstance(doc, str) and doc else None,
                raises=list(raises) if isinstance(raises, list) else [],
                purity=purity_score,
            )
        )

    specs.sort(key=lambda s: s.id)
    return specs


def _split_callees(graph: RepoGraph, func_id: str) -> tuple[list[str], list[str]]:
    """Partition CALLS edges into (calls, constructs).

    A call whose target is a Class node is a construction: it resolves to
    the class's ``__init__`` component when one is defined, else the class
    qualname lands in ``constructs`` (dataclass / ORM style).
    """
    calls: set[str] = set()
    constructs: set[str] = set()
    for edge in graph.out_edges(func_id, EdgeKind.CALLS):
        callee = graph.get_node(edge.dst)
        if callee.kind == NodeKind.Class:
            init = next(
                (m for m in graph.children(callee.id, NodeKind.Method) if m.name == "__init__"),
                None,
            )
            if init is not None:
                calls.add(_callee_label(graph, init.id))
            else:
                constructs.add(_callee_label(graph, callee.id))
        else:
            calls.add(_callee_label(graph, edge.dst))
    return sorted(calls), sorted(constructs)


def _callee_label(graph: RepoGraph, node_id: str) -> str:
    node = graph.get_node(node_id)
    qual = node.attrs.get("qualname") if node.attrs else None
    return str(qual) if isinstance(qual, str) else node.name


def _has_mutations(graph: RepoGraph, func_id: str) -> bool:
    """True if any CFG child mutates state *observable by the caller*.

    Covers attribute/subscript assignment LHS (``self.x = v``, ``xs[0] = v``,
    ``self.total += n``) on ``Assignment`` nodes and bare mutator method
    calls (``xs.append(x)``) on ``Statement`` nodes — both populated by
    :mod:`cgir.analyses.cfg` via ``attrs["mutates"]``.

    Mutating an object the function *itself created* (a local) is invisible
    to callers and stays pure: a mutated base name only counts if it is a
    parameter, ``self``, or a name never written locally (a global).
    """
    params = {p.name for p in graph.children(func_id, NodeKind.Parameter)}
    local_writes: set[str] = set()
    mutated: list[str] = []
    for child in graph.children(func_id):
        attrs = child.attrs or {}
        writes = attrs.get("writes")
        if isinstance(writes, list):
            local_writes.update(writes)
        mutates = attrs.get("mutates")
        if isinstance(mutates, list):
            mutated.extend(mutates)
    return any(base in params or base not in local_writes for base in mutated)


def _classify(effects: list[str], purity_score: float, mutates_state: bool) -> ComponentKind:
    tags = set(effects)
    if tags & IMPURE_EFFECT_TAGS:
        return ComponentKind.effect_adapter
    if TRANSITIVE_TAG in tags:
        return ComponentKind.orchestrator
    if mutates_state:
        return ComponentKind.state_transformer
    if purity_score == 1.0:
        return ComponentKind.pure_function
    return ComponentKind.unknown
