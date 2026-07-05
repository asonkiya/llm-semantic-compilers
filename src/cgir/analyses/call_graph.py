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
        for callee_name, arg_names, line in _call_sites(func_ts, source):
            target = _resolve_callee(tables, table, callee_name)
            if target is None:
                continue
            # Note: multi-edges collapse per (caller, callee) pair, so the
            # recorded args/line describe one representative call site.
            graph.add_edge(
                Edge(
                    src=func.id,
                    dst=target,
                    kind=EdgeKind.CALLS,
                    attrs={"args": arg_names, "line": line},
                )
            )


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


def _call_sites(func_node: TSNode, source: bytes) -> list[tuple[str, list[str], int]]:
    """Each call in the body: ``(dotted_callee, arg_identifier_names, line)``."""
    sites: list[tuple[str, list[str], int]] = []
    body = func_node.child_by_field_name("body")
    if body is None:
        return sites
    stack: list[TSNode] = [body]
    while stack:
        node = stack.pop()
        if node.type == "call":
            function_field = node.child_by_field_name("function")
            if function_field is not None:
                if function_field.type == "identifier":
                    text = source[function_field.start_byte : function_field.end_byte]
                    decoded = text.decode("utf-8", errors="replace")
                elif function_field.type == "attribute":
                    text = source[function_field.start_byte : function_field.end_byte]
                    decoded = text.decode("utf-8", errors="replace")
                    if "(" in decoded or "[" in decoded or "\n" in decoded:
                        # Computed receiver: keep just the head identifier.
                        decoded = decoded.split(".", 1)[0]
                else:
                    decoded = None
                if decoded:
                    arguments = node.child_by_field_name("arguments")
                    args = _arg_names(arguments, source) if arguments is not None else []
                    sites.append((decoded, args, node.start_point[0] + 1))
        stack.extend(node.children)
    return sites


def _arg_names(args_node: TSNode, source: bytes) -> list[str]:
    """Data identifiers read inside a call's argument list.

    Attribute names and nested callee names are excluded — only names that
    carry data count (mirrors the CFG ``reads`` rules).
    """
    names: list[str] = []
    seen: set[str] = set()

    def collect(node: TSNode) -> None:
        if node.type == "identifier":
            text = source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
            if text not in seen:
                seen.add(text)
                names.append(text)
            return
        if node.type == "attribute":
            obj = node.child_by_field_name("object")
            if obj is not None:
                collect(obj)
            return
        if node.type == "call":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "attribute":
                obj = fn.child_by_field_name("object")
                if obj is not None:
                    collect(obj)
            inner = node.child_by_field_name("arguments")
            if inner is not None:
                collect(inner)
            return
        for child in node.children:
            collect(child)

    collect(args_node)
    return names
