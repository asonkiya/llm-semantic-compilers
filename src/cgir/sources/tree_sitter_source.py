"""Tree-sitter ingester for Python.

Walks a repo, parses every ``.py`` file with tree-sitter, and emits the
``Repository → File → Module → (Class → Method | Function)`` containment
spine plus ``Parameter`` children and per-function ``Import`` siblings.

This pass only builds the structural skeleton. Symbol resolution and the
``CALLS`` edges live in :mod:`cgir.analyses.symbols` and
:mod:`cgir.analyses.call_graph` so that downstream backends (Joern, CodeQL)
can plug in at the same seam.
"""

from __future__ import annotations

from pathlib import Path

import tree_sitter_python
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode

from cgir.ir.edges import Edge, EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import Node, NodeKind
from cgir.sources.base import GraphSource


def _python_parser() -> Parser:
    language = Language(tree_sitter_python.language())
    parser = Parser()
    parser.language = language
    return parser


class TreeSitterSource(GraphSource):
    def __init__(self) -> None:
        self._parser = _python_parser()

    def ingest(self, repo_path: Path) -> RepoGraph:
        repo_path = repo_path.resolve()
        graph = RepoGraph()
        repo_id = f"repo:{repo_path.name}"
        graph.add_node(
            Node(id=repo_id, kind=NodeKind.Repository, name=repo_path.name, path=str(repo_path))
        )
        for py_file in sorted(repo_path.rglob("*.py")):
            if any(part.startswith(".") for part in py_file.relative_to(repo_path).parts):
                continue
            self._ingest_file(graph, repo_path, repo_id, py_file)
        return graph

    def _ingest_file(
        self, graph: RepoGraph, repo_path: Path, repo_id: str, file_path: Path
    ) -> None:
        rel = file_path.relative_to(repo_path)
        rel_str = str(rel)
        module_name = ".".join(rel.with_suffix("").parts)
        file_id = f"file:{rel_str}"
        module_id = f"module:{module_name}"

        source = file_path.read_bytes()
        tree = self._parser.parse(source)
        root = tree.root_node

        graph.add_node(
            Node(
                id=file_id,
                kind=NodeKind.File,
                name=rel_str,
                path=rel_str,
                start_line=1,
                end_line=root.end_point[0] + 1,
            )
        )
        graph.add_edge(Edge(src=repo_id, dst=file_id, kind=EdgeKind.CONTAINS))

        graph.add_node(
            Node(
                id=module_id,
                kind=NodeKind.Module,
                name=module_name,
                path=rel_str,
                start_line=1,
                end_line=root.end_point[0] + 1,
                attrs={"language": "python"},
            )
        )
        graph.add_edge(Edge(src=file_id, dst=module_id, kind=EdgeKind.CONTAINS))

        self._walk_module(graph, module_id, module_name, rel_str, source, root)

    def _walk_module(
        self,
        graph: RepoGraph,
        module_id: str,
        module_name: str,
        rel_path: str,
        source: bytes,
        root: TSNode,
    ) -> None:
        for child in root.children:
            if child.type == "function_definition":
                self._add_function(graph, module_id, module_name, rel_path, source, child)
            elif child.type == "class_definition":
                self._add_class(graph, module_id, module_name, rel_path, source, child)
            elif child.type in {"import_statement", "import_from_statement"}:
                self._add_imports(graph, module_id, rel_path, source, child)

    def _add_function(
        self,
        graph: RepoGraph,
        parent_id: str,
        parent_qual: str,
        rel_path: str,
        source: bytes,
        ts_node: TSNode,
    ) -> str:
        name = _identifier_text(ts_node.child_by_field_name("name"), source) or "<anonymous>"
        is_method = parent_id.startswith("class:")
        qual = f"{parent_qual}.{name}"
        kind = NodeKind.Method if is_method else NodeKind.Function
        prefix = "method" if is_method else "func"
        node_id = f"{prefix}:{qual}"
        graph.add_node(
            Node(
                id=node_id,
                kind=kind,
                name=name,
                path=rel_path,
                start_line=ts_node.start_point[0] + 1,
                end_line=ts_node.end_point[0] + 1,
                attrs={"qualname": qual, "signature": _signature_text(ts_node, source)},
            )
        )
        graph.add_edge(Edge(src=parent_id, dst=node_id, kind=EdgeKind.CONTAINS))
        self._add_parameters(graph, node_id, qual, rel_path, source, ts_node)
        return node_id

    def _add_class(
        self,
        graph: RepoGraph,
        parent_id: str,
        parent_qual: str,
        rel_path: str,
        source: bytes,
        ts_node: TSNode,
    ) -> None:
        name = _identifier_text(ts_node.child_by_field_name("name"), source) or "<anonymous>"
        qual = f"{parent_qual}.{name}"
        node_id = f"class:{qual}"
        graph.add_node(
            Node(
                id=node_id,
                kind=NodeKind.Class,
                name=name,
                path=rel_path,
                start_line=ts_node.start_point[0] + 1,
                end_line=ts_node.end_point[0] + 1,
                attrs={"qualname": qual},
            )
        )
        graph.add_edge(Edge(src=parent_id, dst=node_id, kind=EdgeKind.CONTAINS))
        body = ts_node.child_by_field_name("body")
        if body is None:
            return
        for child in body.children:
            if child.type == "function_definition":
                self._add_function(graph, node_id, qual, rel_path, source, child)

    def _add_parameters(
        self,
        graph: RepoGraph,
        func_id: str,
        func_qual: str,
        rel_path: str,
        source: bytes,
        ts_node: TSNode,
    ) -> None:
        params = ts_node.child_by_field_name("parameters")
        if params is None:
            return
        for child in params.children:
            param_name = _param_name(child, source)
            if param_name is None:
                continue
            if param_name == "self" and func_id.startswith("method:"):
                continue
            param_id = f"param:{func_qual}.{param_name}"
            graph.add_node(
                Node(
                    id=param_id,
                    kind=NodeKind.Parameter,
                    name=param_name,
                    path=rel_path,
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                )
            )
            graph.add_edge(Edge(src=func_id, dst=param_id, kind=EdgeKind.CONTAINS))

    def _add_imports(
        self,
        graph: RepoGraph,
        module_id: str,
        rel_path: str,
        source: bytes,
        ts_node: TSNode,
    ) -> None:
        for target in _import_targets(ts_node, source):
            import_id = f"import:{module_id}::{target}"
            graph.add_node(
                Node(
                    id=import_id,
                    kind=NodeKind.Import,
                    name=target,
                    path=rel_path,
                    start_line=ts_node.start_point[0] + 1,
                    end_line=ts_node.end_point[0] + 1,
                    attrs={"target": target},
                )
            )
            graph.add_edge(Edge(src=module_id, dst=import_id, kind=EdgeKind.CONTAINS))
            graph.add_edge(
                Edge(src=module_id, dst=import_id, kind=EdgeKind.IMPORTS, attrs={"target": target})
            )


def _identifier_text(node: TSNode | None, source: bytes) -> str | None:
    if node is None:
        return None
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _signature_text(func_node: TSNode, source: bytes) -> str:
    name = _identifier_text(func_node.child_by_field_name("name"), source) or ""
    params_node = func_node.child_by_field_name("parameters")
    params_text = _identifier_text(params_node, source) or "()"
    return_node = func_node.child_by_field_name("return_type")
    return_text = _identifier_text(return_node, source)
    sig = f"{name}{params_text}"
    if return_text:
        sig += f" -> {return_text}"
    return sig


def _param_name(node: TSNode, source: bytes) -> str | None:
    if node.type == "identifier":
        return _identifier_text(node, source)
    if node.type in {
        "typed_parameter",
        "default_parameter",
        "typed_default_parameter",
        "list_splat_pattern",
        "dictionary_splat_pattern",
    }:
        # Search children for the identifier that names the parameter.
        for child in node.children:
            if child.type == "identifier":
                return _identifier_text(child, source)
    return None


def _import_targets(node: TSNode, source: bytes) -> list[str]:
    targets: list[str] = []
    if node.type == "import_statement":
        for child in node.children:
            if child.type in {"dotted_name", "aliased_import"}:
                name = _identifier_text(_name_child(child), source)
                if name:
                    targets.append(name)
    elif node.type == "import_from_statement":
        module_node = node.child_by_field_name("module_name")
        module = _identifier_text(module_node, source) or ""
        for child in node.children_by_field_name("name"):
            name = _identifier_text(_name_child(child), source)
            if name:
                targets.append(f"{module}.{name}" if module else name)
    return targets


def _name_child(node: TSNode) -> TSNode:
    if node.type == "aliased_import":
        sub = node.child_by_field_name("name")
        if sub is not None:
            return sub
    return node


__all__ = ["TreeSitterSource"]
