"""Build CALLS edges from resolved symbol tables.

Walks each function/method body with tree-sitter (files parsed once via
:class:`~cgir.analyses._python_ast.SourceCache`), finds ``call``
expressions, and resolves the callee name through the owning module's
symbol table. Unresolved calls are dropped — third-party effects show
up via :mod:`cgir.analyses.effects` instead.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node as TSNode

from cgir.analyses._python_ast import SourceCache, locate_function, python_parser
from cgir.analyses.symbols import SymbolTable, module_of
from cgir.ir.edges import Edge, EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import NodeKind


def build_call_graph(graph: RepoGraph, tables: dict[str, SymbolTable], repo_path: Path) -> None:
    cache = SourceCache(python_parser(), repo_path)
    for func in list(graph.nodes()):
        if func.kind not in {NodeKind.Function, NodeKind.Method}:
            continue
        module_id = module_of(graph, func)
        if module_id is None or func.path is None:
            continue
        table = tables.get(module_id)
        if table is None:
            continue
        parsed = cache.get(func.path)
        if parsed is None:
            continue
        source, root = parsed
        func_ts = locate_function(root, func.name, (func.start_line or 1) - 1)
        if func_ts is None:
            continue
        for callee_name in _call_names(func_ts, source):
            target = _resolve_callee(tables, table, callee_name)
            if target is None:
                continue
            graph.add_edge(Edge(src=func.id, dst=target, kind=EdgeKind.CALLS))


def _resolve_callee(tables: dict[str, SymbolTable], table: SymbolTable, dotted: str) -> str | None:
    """Resolve a (possibly dotted) callee through the local symbol table.

    ``chapter.get_chapter`` where ``chapter`` binds to a module resolves
    into that module's own table — the edge lands on the function, not the
    module. Non-module attribute bases (``self.repo.get``) stay at the
    binding of the head, which is usually unbound and dropped.
    """
    head, _, rest = dotted.partition(".")
    target = table.bindings.get(head)
    if target is None:
        return None
    if rest and target.startswith("module:"):
        sub = tables.get(target)
        if sub is None:
            return None
        return sub.bindings.get(rest.split(".", 1)[0])
    return target


def _call_names(func_node: TSNode, source: bytes) -> list[str]:
    names: list[str] = []
    body = func_node.child_by_field_name("body")
    if body is None:
        return names
    stack: list[TSNode] = [body]
    while stack:
        node = stack.pop()
        if node.type == "call":
            function_field = node.child_by_field_name("function")
            if function_field is not None:
                if function_field.type == "identifier":
                    text = source[function_field.start_byte : function_field.end_byte]
                    names.append(text.decode("utf-8", errors="replace"))
                elif function_field.type == "attribute":
                    text = source[function_field.start_byte : function_field.end_byte]
                    decoded = text.decode("utf-8", errors="replace")
                    if "(" in decoded or "[" in decoded or "\n" in decoded:
                        # Computed receiver: keep just the head identifier.
                        decoded = decoded.split(".", 1)[0]
                    names.append(decoded)
        stack.extend(node.children)
    return names
