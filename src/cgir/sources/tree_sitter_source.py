"""Tree-sitter ingester for Python.

Walks a repo, parses every ``.py`` file with tree-sitter, and emits the
``Repository → File → Module → (Class → Method | Function)`` containment
spine plus ``Parameter`` children and per-module ``Import`` siblings.

This pass only builds the structural skeleton. Symbol resolution and the
``CALLS`` edges live in :mod:`cgir.analyses.symbols` and
:mod:`cgir.analyses.call_graph` so that downstream backends (Joern, CodeQL)
can plug in at the same seam.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import tree_sitter_python
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode

from cgir.ir.edges import Edge, EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import Node, NodeKind
from cgir.sources.base import GraphSource

DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset(
    {
        # Virtual environments
        "venv",
        "env",
        # Build / dist artefacts
        "build",
        "dist",
        "target",
        "out",
        # Python caches
        "__pycache__",
        "site-packages",
        # Tool caches (dot-prefixed names are also covered by the dot-prefix
        # filter below; listed here for completeness when authors rename them)
        ".tox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        # Other ecosystems sometimes vendored into Python repos
        "node_modules",
    }
)


def _python_parser() -> Parser:
    language = Language(tree_sitter_python.language())
    parser = Parser()
    parser.language = language
    return parser


class TreeSitterSource(GraphSource):
    def __init__(self, ignore_dirs: Iterable[str] | None = None) -> None:
        self._parser = _python_parser()
        self._ignore: frozenset[str] = DEFAULT_IGNORE_DIRS | frozenset(ignore_dirs or ())

    def ingest(self, repo_path: Path) -> RepoGraph:
        repo_path = repo_path.resolve()
        graph = RepoGraph()
        repo_id = f"repo:{repo_path.name}"
        graph.add_node(
            Node(id=repo_id, kind=NodeKind.Repository, name=repo_path.name, path=str(repo_path))
        )
        for py_file in sorted(repo_path.rglob("*.py")):
            if self._should_skip(py_file.relative_to(repo_path)):
                continue
            self._ingest_file(graph, repo_path, repo_id, py_file)
        return graph

    def _should_skip(self, rel: Path) -> bool:
        for part in rel.parts:
            if part.startswith("."):
                return True
            if part in self._ignore:
                return True
        return False

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
            self._dispatch_top_level(graph, module_id, module_name, rel_path, source, child)

    def _dispatch_top_level(
        self,
        graph: RepoGraph,
        module_id: str,
        module_name: str,
        rel_path: str,
        source: bytes,
        ts_node: TSNode,
    ) -> None:
        if ts_node.type == "function_definition":
            self._add_function(graph, module_id, module_name, rel_path, source, ts_node)
        elif ts_node.type == "class_definition":
            self._add_class(graph, module_id, module_name, rel_path, source, ts_node)
        elif ts_node.type == "decorated_definition":
            inner = _undecorated(ts_node)
            if inner is None:
                return
            decorators = _decorator_texts(ts_node, source)
            if inner.type == "function_definition":
                self._add_function(
                    graph, module_id, module_name, rel_path, source, inner, decorators
                )
            else:
                self._dispatch_top_level(graph, module_id, module_name, rel_path, source, inner)
        elif ts_node.type in {"import_statement", "import_from_statement"}:
            self._add_imports(graph, module_id, module_name, rel_path, source, ts_node)
        elif ts_node.type == "expression_statement":
            self._add_module_variables(graph, module_id, module_name, rel_path, source, ts_node)

    def _add_module_variables(
        self,
        graph: RepoGraph,
        module_id: str,
        module_name: str,
        rel_path: str,
        source: bytes,
        ts_node: TSNode,
    ) -> None:
        """Module-level assignments become ``Variable`` nodes (constants, aliases).

        These carry a source span so :mod:`cgir.report.pack` can include a
        ``Point: TypeAlias = tuple[float, float]`` line when a component's
        contract references ``Point``.
        """
        assign = next((c for c in ts_node.children if c.type == "assignment"), None)
        if assign is None:
            return
        left = assign.child_by_field_name("left")
        if left is None:
            return
        for name in _assignment_target_names(left, source):
            var_id = f"var:{module_name}.{name}"
            graph.add_node(
                Node(
                    id=var_id,
                    kind=NodeKind.Variable,
                    name=name,
                    path=rel_path,
                    start_line=ts_node.start_point[0] + 1,
                    end_line=ts_node.end_point[0] + 1,
                    attrs={"qualname": f"{module_name}.{name}"},
                )
            )
            graph.add_edge(Edge(src=module_id, dst=var_id, kind=EdgeKind.CONTAINS))

    def _add_function(
        self,
        graph: RepoGraph,
        parent_id: str,
        parent_qual: str,
        rel_path: str,
        source: bytes,
        ts_node: TSNode,
        decorators: list[str] | None = None,
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
                attrs={
                    "qualname": qual,
                    "signature": _signature_text(ts_node, source),
                    "returns": _return_annotation_text(ts_node, source),
                    "decorators": list(decorators or []),
                    "doc": _docstring_text(ts_node, source),
                    "raises": _raised_names(ts_node, source),
                },
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
            self._dispatch_in_class(graph, node_id, qual, rel_path, source, child)

    def _dispatch_in_class(
        self,
        graph: RepoGraph,
        class_id: str,
        class_qual: str,
        rel_path: str,
        source: bytes,
        ts_node: TSNode,
    ) -> None:
        if ts_node.type == "function_definition":
            self._add_function(graph, class_id, class_qual, rel_path, source, ts_node)
        elif ts_node.type == "decorated_definition":
            inner = _undecorated(ts_node)
            if inner is not None and inner.type == "function_definition":
                self._add_function(
                    graph,
                    class_id,
                    class_qual,
                    rel_path,
                    source,
                    inner,
                    _decorator_texts(ts_node, source),
                )

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
        current_module: str,
        rel_path: str,
        source: bytes,
        ts_node: TSNode,
    ) -> None:
        for target, alias in _import_targets(ts_node, source, current_module):
            import_id = f"import:{module_id}::{target}"
            graph.add_node(
                Node(
                    id=import_id,
                    kind=NodeKind.Import,
                    name=target,
                    path=rel_path,
                    start_line=ts_node.start_point[0] + 1,
                    end_line=ts_node.end_point[0] + 1,
                    attrs={"target": target, "alias": alias},
                )
            )
            graph.add_edge(Edge(src=module_id, dst=import_id, kind=EdgeKind.CONTAINS))
            graph.add_edge(
                Edge(src=module_id, dst=import_id, kind=EdgeKind.IMPORTS, attrs={"target": target})
            )


def _assignment_target_names(left: TSNode, source: bytes) -> list[str]:
    """Bound names on an assignment LHS: ``x``, ``x, y``; attribute/subscript LHS none."""
    if left.type == "identifier":
        text = _identifier_text(left, source)
        return [text] if text else []
    if left.type in {"pattern_list", "tuple_pattern", "list_pattern"}:
        names: list[str] = []
        for child in left.named_children:
            names.extend(_assignment_target_names(child, source))
        return names
    return []


def _undecorated(decorated_ts: TSNode) -> TSNode | None:
    """Return the function/class wrapped by a ``decorated_definition``."""
    for child in decorated_ts.named_children:
        if child.type in {"function_definition", "class_definition"}:
            return child
    return None


def _decorator_texts(decorated_ts: TSNode, source: bytes) -> list[str]:
    """Each decorator's call text, without the leading ``@``."""
    texts: list[str] = []
    for child in decorated_ts.named_children:
        if child.type == "decorator":
            raw = source[child.start_byte : child.end_byte].decode("utf-8", errors="replace")
            texts.append(raw.lstrip("@").strip())
    return texts


def _identifier_text(node: TSNode | None, source: bytes) -> str | None:
    if node is None:
        return None
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _return_annotation_text(func_node: TSNode, source: bytes) -> str | None:
    """The declared return type (``-> float`` gives ``"float"``), if any."""
    return_node = func_node.child_by_field_name("return_type")
    return _identifier_text(return_node, source)


def _docstring_text(func_node: TSNode, source: bytes) -> str:
    """The function's docstring (first string statement in the body), cleaned."""
    body = func_node.child_by_field_name("body")
    if body is None:
        return ""
    for stmt in body.named_children:
        if stmt.type == "comment":
            continue
        if stmt.type == "expression_statement" and stmt.named_children:
            inner = stmt.named_children[0]
            if inner.type == "string":
                raw = source[inner.start_byte : inner.end_byte].decode("utf-8", errors="replace")
                return _clean_docstring(raw)
        return ""  # first real statement isn't a string → no docstring
    return ""


def _clean_docstring(raw: str) -> str:
    text = raw.strip()
    for quote in ('"""', "'''", '"', "'"):
        if text.startswith(quote):
            text = text[len(quote) :]
            if text.endswith(quote):
                text = text[: -len(quote)]
            break
    return text.strip()


def _raised_names(func_node: TSNode, source: bytes) -> list[str]:
    """Exception class names raised in the body (``raise ValueError(...)`` → ValueError)."""
    body = func_node.child_by_field_name("body")
    if body is None:
        return []
    names: list[str] = []
    seen: set[str] = set()
    stack: list[TSNode] = [body]
    while stack:
        node = stack.pop()
        if node.type == "raise_statement":
            for child in node.named_children:
                target = child.child_by_field_name("function") if child.type == "call" else child
                if target is None:
                    continue
                text = source[target.start_byte : target.end_byte].decode("utf-8", errors="replace")
                name = text.split(".")[-1].split("(")[0].strip()
                if name and name[0].isupper() and name not in seen:
                    seen.add(name)
                    names.append(name)
                break
        stack.extend(node.children)
    return names


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


def _import_targets(
    node: TSNode, source: bytes, current_module: str
) -> list[tuple[str, str | None]]:
    """Yield ``(absolute_target, local_alias_or_None)`` per imported name."""
    targets: list[tuple[str, str | None]] = []
    if node.type == "import_statement":
        for child in node.children:
            if child.type in {"dotted_name", "aliased_import"}:
                name = _identifier_text(_name_child(child), source)
                if name:
                    targets.append((name, _alias_text(child, source)))
    elif node.type == "import_from_statement":
        module_node = node.child_by_field_name("module_name")
        module = _resolve_from_module(module_node, source, current_module) if module_node else ""
        for child in node.children_by_field_name("name"):
            name = _identifier_text(_name_child(child), source)
            if name:
                target = f"{module}.{name}" if module else name
                targets.append((target, _alias_text(child, source)))
    return targets


def _alias_text(ts_node: TSNode, source: bytes) -> str | None:
    """The ``as`` alias of an ``aliased_import``, if any."""
    if ts_node.type != "aliased_import":
        return None
    return _identifier_text(ts_node.child_by_field_name("alias"), source)


def _resolve_from_module(module_node: TSNode, source: bytes, current_module: str) -> str:
    """Return the absolute dotted name of an ``import_from_statement`` module.

    Handles both absolute (``from a.b import x``) and relative
    (``from .a import x``, ``from ..a.b import x``) forms.
    """
    if module_node.type == "relative_import":
        dots = 0
        sub_name = ""
        for child in module_node.children:
            if child.type == "import_prefix":
                # import_prefix's text is one or more "." characters.
                raw = source[child.start_byte : child.end_byte]
                dots = raw.count(b".")
            elif child.type == "dotted_name":
                sub_name = _identifier_text(child, source) or ""
        if dots == 0:
            return sub_name
        parts = current_module.split(".")
        # Drop the module itself; each extra dot peels off one more package level.
        package_parts = parts[:-1]
        up = dots - 1
        if up > 0:
            package_parts = package_parts[:-up] if up <= len(package_parts) else []
        absolute = list(package_parts)
        if sub_name:
            absolute.extend(sub_name.split("."))
        return ".".join(absolute)
    return _identifier_text(module_node, source) or ""


def _name_child(node: TSNode) -> TSNode:
    if node.type == "aliased_import":
        sub = node.child_by_field_name("name")
        if sub is not None:
            return sub
    return node


__all__ = ["DEFAULT_IGNORE_DIRS", "TreeSitterSource"]
