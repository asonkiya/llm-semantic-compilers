"""Side-effect classification for Python functions.

Effect taxonomy:
    io              calls into ``print``, ``input``, ``open``
    raise           contains a ``raise`` statement
    calls_effectful (transitive only) — a callee has a direct effect

The transitive tag is split out from the direct ones so :mod:`cgir.slicing`
can distinguish ``effect_adapter`` (does IO itself) from ``orchestrator``
(only routes calls to effectful components).

Future milestones will extend the direct taxonomy with ``net``, ``fs``, and
``nondeterm`` — the closure logic here already treats any tag in
:data:`DIRECT_EFFECT_TAGS` as effectful so new tags drop in without
changes elsewhere.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node as TSNode
from tree_sitter import Parser

from cgir.analyses._python_ast import locate_function, python_parser
from cgir.ir.edges import EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import NodeKind

DIRECT_EFFECT_TAGS: frozenset[str] = frozenset({"io", "raise", "net", "fs", "nondeterm"})
TRANSITIVE_TAG = "calls_effectful"

_IO_BUILTINS: frozenset[str] = frozenset({"print", "input", "open"})


def classify(graph: RepoGraph, repo_path: Path) -> dict[str, list[str]]:
    """Return ``{function_id: sorted([effect_tag, ...])}`` for every function/method."""
    parser = python_parser()
    func_nodes = [n for n in graph.nodes() if n.kind in {NodeKind.Function, NodeKind.Method}]

    effects: dict[str, set[str]] = {}
    for func in func_nodes:
        effects[func.id] = _direct_effects(parser, repo_path, func)

    # Propagate transitively over CALLS edges until fixed point.
    changed = True
    while changed:
        changed = False
        for func in func_nodes:
            for edge in graph.out_edges(func.id, EdgeKind.CALLS):
                callee = effects.get(edge.dst, set())
                if (
                    callee & DIRECT_EFFECT_TAGS or TRANSITIVE_TAG in callee
                ) and TRANSITIVE_TAG not in effects[func.id]:
                    effects[func.id].add(TRANSITIVE_TAG)
                    changed = True

    return {nid: sorted(tags) for nid, tags in effects.items()}


def _direct_effects(parser: Parser, repo_path: Path, func: object) -> set[str]:
    # ``func`` is a :class:`cgir.ir.nodes.Node`; typed loosely to avoid an
    # import cycle with the slicer.
    path = getattr(func, "path", None)
    start_line = getattr(func, "start_line", None)
    name = getattr(func, "name", None)
    if path is None or start_line is None or name is None:
        return set()
    try:
        source = (repo_path / path).read_bytes()
    except OSError:
        return set()
    tree = parser.parse(source)
    func_ts = locate_function(tree.root_node, name, start_line - 1)
    if func_ts is None:
        return set()
    return _walk_body_for_effects(func_ts, source)


def _walk_body_for_effects(func_ts: TSNode, source: bytes) -> set[str]:
    tags: set[str] = set()
    body = func_ts.child_by_field_name("body")
    if body is None:
        return tags
    stack: list[TSNode] = [body]
    while stack:
        node = stack.pop()
        if node.type == "raise_statement":
            tags.add("raise")
        elif node.type == "call":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "identifier":
                name = source[fn.start_byte : fn.end_byte].decode("utf-8", errors="replace")
                if name in _IO_BUILTINS:
                    tags.add("io")
        stack.extend(node.children)
    return tags
