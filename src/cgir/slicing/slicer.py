"""Slice each Function/Method node into a ComponentSpec."""

from __future__ import annotations

from cgir.analyses.effects import DIRECT_EFFECT_TAGS, TRANSITIVE_TAG
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
        calls = sorted(
            {_callee_label(graph, edge.dst) for edge in graph.out_edges(node.id, EdgeKind.CALLS)}
        )
        node_effects = effects.get(node.id, [])
        purity_score = purity_scores.get(node.id, PLACEHOLDER_SCORE)
        mutates_state = _has_mutations(graph, node.id)
        kind = _classify(node_effects, purity_score, mutates_state)

        trace = []
        if node.path is not None and node.start_line is not None:
            trace.append(f"{node.path}:{node.start_line}")

        signature = node.attrs.get("signature") if node.attrs else None

        specs.append(
            ComponentSpec(
                id=qual,
                kind=kind,
                inputs=inputs,
                outputs=[],
                effects=list(node_effects),
                calls=calls,
                trace=trace,
                language=language,
                signature=signature if isinstance(signature, str) else None,
                purity=purity_score,
            )
        )

    specs.sort(key=lambda s: s.id)
    return specs


def _callee_label(graph: RepoGraph, node_id: str) -> str:
    node = graph.get_node(node_id)
    qual = node.attrs.get("qualname") if node.attrs else None
    return str(qual) if isinstance(qual, str) else node.name


def _has_mutations(graph: RepoGraph, func_id: str) -> bool:
    """True if any Assignment child mutates an existing object (attr/subscript LHS).

    Populated by :mod:`cgir.analyses.cfg` via ``Assignment.attrs["mutates"]``.
    """
    for child in graph.children(func_id, NodeKind.Assignment):
        mutates = child.attrs.get("mutates") if child.attrs else None
        if isinstance(mutates, list) and mutates:
            return True
    return False


def _classify(effects: list[str], purity_score: float, mutates_state: bool) -> ComponentKind:
    tags = set(effects)
    if tags & DIRECT_EFFECT_TAGS:
        return ComponentKind.effect_adapter
    if TRANSITIVE_TAG in tags:
        return ComponentKind.orchestrator
    if mutates_state:
        return ComponentKind.state_transformer
    if purity_score == 1.0:
        return ComponentKind.pure_function
    return ComponentKind.unknown
