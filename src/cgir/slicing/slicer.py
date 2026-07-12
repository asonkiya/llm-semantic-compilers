"""Slice each Function/Method node into a ComponentSpec."""

from __future__ import annotations

from pathlib import Path

from cgir.analyses.effects import IMPURE_EFFECT_TAGS, TRANSITIVE_TAG
from cgir.analyses.entrypoints import detect as detect_entrypoint
from cgir.analyses.purity import PLACEHOLDER_SCORE
from cgir.ir.component_spec import ComponentKind, ComponentSpec
from cgir.ir.edges import EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import Node, NodeKind
from cgir.languages import adapter_for_extension


def _node_language(node: Node, fallback: str) -> str:
    """The component's language, from its file's adapter (multi-language repos)."""
    if node.path:
        adapter = adapter_for_extension(Path(node.path).suffix)
        if adapter is not None:
            return adapter.name
    return fallback


def slice_components(
    graph: RepoGraph,
    effects: dict[str, list[str]] | None = None,
    purity_scores: dict[str, float] | None = None,
    language: str = "python",
    lexical_effects: dict[str, list[str]] | None = None,
) -> list[ComponentSpec]:
    effects = effects or {}
    purity_scores = purity_scores or {}
    lexical_effects = lexical_effects or {}
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
        pins = attrs.get("pins")
        covered_by = _covering_tests(graph, node.id)

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
                language=_node_language(node, language),
                signature=signature if isinstance(signature, str) else None,
                entrypoint=detect_entrypoint(
                    decorators if isinstance(decorators, list) else [], node.name
                ),
                doc=doc if isinstance(doc, str) and doc else None,
                raises=list(raises) if isinstance(raises, list) else [],
                covered_by=covered_by,
                purity=purity_score,
                pins=list(pins) if isinstance(pins, list) else [],
                lexical_effects=sorted(set(lexical_effects.get(node.id, [])) & set(node_effects)),
            )
        )

    specs.sort(key=lambda s: s.id)
    return specs


def _covering_tests(graph: RepoGraph, func_id: str) -> list[str]:
    """Test components that call this one (via resolved CALLS edges)."""
    tests: set[str] = set()
    for edge in graph.in_edges(func_id, EdgeKind.CALLS):
        caller = graph.get_node(edge.src)
        if _is_test_node(caller):
            tests.add(_callee_label(graph, caller.id))
    return sorted(tests)


def _is_test_node(node: Node) -> bool:
    """Heuristic: pytest-style test functions in test files."""
    if node.name.startswith("test_"):
        return True
    if node.path:
        parts = node.path.replace("\\", "/").split("/")
        stem = parts[-1].removesuffix(".py")
        return "tests" in parts[:-1] or stem.startswith("test_") or stem.endswith("_test")
    return False


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
