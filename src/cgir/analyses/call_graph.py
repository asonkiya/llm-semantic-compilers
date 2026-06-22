"""Build CALLS edges from resolved symbol tables.

Re-parses each function/method body with tree-sitter, finds ``call``
expressions, and resolves the callee name through the owning module's
symbol table. Unresolved calls are dropped — third-party effects show
up via :mod:`cgir.analyses.effects` instead.
"""

from __future__ import annotations

from pathlib import Path

import tree_sitter_python
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode

from cgir.analyses.symbols import SymbolTable, module_of
from cgir.ir.edges import Edge, EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import NodeKind


def build_call_graph(graph: RepoGraph, tables: dict[str, SymbolTable], repo_path: Path) -> None:
    parser = _parser()
    for func in list(graph.nodes()):
        if func.kind not in {NodeKind.Function, NodeKind.Method}:
            continue
        module_id = module_of(graph, func)
        if module_id is None or func.path is None:
            continue
        table = tables.get(module_id)
        if table is None:
            continue
        try:
            source = (repo_path / func.path).read_bytes()
        except OSError:
            continue
        tree = parser.parse(source)
        func_ts = _locate_function(tree.root_node, func.name, (func.start_line or 1) - 1)
        if func_ts is None:
            continue
        for callee_name in _call_names(func_ts, source):
            target = table.bindings.get(callee_name)
            if target is None:
                continue
            graph.add_edge(Edge(src=func.id, dst=target, kind=EdgeKind.CALLS))


def _parser() -> Parser:
    language = Language(tree_sitter_python.language())
    parser = Parser()
    parser.language = language
    return parser


def _locate_function(root: TSNode, name: str, start_row: int) -> TSNode | None:
    stack: list[TSNode] = [root]
    while stack:
        node = stack.pop()
        if node.type == "function_definition" and node.start_point[0] == start_row:
            name_node = node.child_by_field_name("name")
            if (
                name_node is not None
                and name_node.text is not None
                and name_node.text.decode("utf-8", errors="replace") == name
            ):
                return node
        stack.extend(node.children)
    return None


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
                    head = decoded.split(".", 1)[0]
                    names.append(head)
        stack.extend(node.children)
    return names
