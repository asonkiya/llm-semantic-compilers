"""Generic tree-sitter ingester — language-neutral over a LanguageAdapter.

Walks a repo, parses each source file with the active adapter, and emits
the ``Repository → File → Module → (Class → Method | Function)``
containment spine plus ``Parameter`` children, per-module ``Import``
siblings, and module-level ``Variable`` nodes.

All grammar-specific extraction (what a function/class/import looks like,
signatures, docstrings, raised names, free names, relative-import
resolution) comes from :meth:`LanguageAdapter.module_declarations` as
normalized declarations; this module only builds graph nodes and edges.
Symbol resolution and ``CALLS`` edges live in :mod:`cgir.analyses`.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from tree_sitter import Node as TSNode

from cgir.ir.edges import Edge, EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import Node, NodeKind
from cgir.languages import ADAPTERS, LanguageAdapter, adapter_for_extension
from cgir.languages.base import ClassDecl, FunctionDecl, ImportDecl, VariableDecl
from cgir.languages.cache import parse_cached
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
        # Other ecosystems sometimes vendored into repos
        "node_modules",
    }
)


class TreeSitterSource(GraphSource):
    def __init__(
        self,
        ignore_dirs: Iterable[str] | None = None,
        adapter: LanguageAdapter | None = None,
    ) -> None:
        # adapter=None → dispatch per file extension across all registered
        # languages; a forced adapter restricts ingest to its extensions.
        self._forced = adapter
        self._ignore: frozenset[str] = DEFAULT_IGNORE_DIRS | frozenset(ignore_dirs or ())

    def _extensions(self) -> tuple[str, ...]:
        if self._forced is not None:
            return self._forced.file_extensions
        return tuple(ext for a in ADAPTERS.values() for ext in a.file_extensions)

    def ingest(self, repo_path: Path) -> RepoGraph:
        repo_path = repo_path.resolve()
        graph = RepoGraph()
        repo_id = f"repo:{repo_path.name}"
        graph.add_node(
            Node(id=repo_id, kind=NodeKind.Repository, name=repo_path.name, path=str(repo_path))
        )
        files: list[Path] = []
        for ext in self._extensions():
            files.extend(repo_path.rglob(f"*{ext}"))
        for source_file in sorted(set(files)):
            if self._should_skip(source_file.relative_to(repo_path)):
                continue
            adapter = self._forced or adapter_for_extension(source_file.suffix)
            if adapter is not None:
                self._ingest_file(graph, repo_path, repo_id, source_file, adapter)
        return graph

    def _should_skip(self, rel: Path) -> bool:
        for part in rel.parts:
            if part.startswith("."):
                return True
            if part in self._ignore:
                return True
        return False

    def _ingest_file(
        self,
        graph: RepoGraph,
        repo_path: Path,
        repo_id: str,
        file_path: Path,
        adapter: LanguageAdapter,
    ) -> None:
        rel = file_path.relative_to(repo_path)
        rel_str = str(rel)
        module_name = ".".join(rel.with_suffix("").parts)
        file_id = f"file:{rel_str}"
        module_id = f"module:{module_name}"

        source = file_path.read_bytes()
        root = parse_cached(adapter, source)

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
                attrs={"language": adapter.name},
            )
        )
        graph.add_edge(Edge(src=file_id, dst=module_id, kind=EdgeKind.CONTAINS))

        for decl in adapter.module_declarations(root, source, module_name, rel_str):
            if isinstance(decl, FunctionDecl):
                self._add_function(graph, module_id, module_name, rel_str, decl, is_method=False)
            elif isinstance(decl, ClassDecl):
                self._add_class(graph, module_id, module_name, rel_str, decl)
            elif isinstance(decl, ImportDecl):
                self._add_import(graph, module_id, rel_str, decl)
            elif isinstance(decl, VariableDecl):
                self._add_variable(graph, module_id, module_name, rel_str, decl)

    def _add_function(
        self,
        graph: RepoGraph,
        parent_id: str,
        parent_qual: str,
        rel_path: str,
        decl: FunctionDecl,
        is_method: bool,
    ) -> None:
        qual = f"{parent_qual}.{decl.name}"
        kind = NodeKind.Method if is_method else NodeKind.Function
        prefix = "method" if is_method else "func"
        node_id = f"{prefix}:{qual}"
        graph.add_node(
            Node(
                id=node_id,
                kind=kind,
                name=decl.name,
                path=rel_path,
                start_line=decl.node.start_point[0] + 1,
                end_line=decl.node.end_point[0] + 1,
                attrs={
                    "qualname": qual,
                    "signature": decl.signature,
                    "returns": decl.returns,
                    "decorators": list(decl.decorators),
                    "doc": decl.doc,
                    "raises": list(decl.raises),
                    "free_names": list(decl.free_names),
                },
            )
        )
        graph.add_edge(Edge(src=parent_id, dst=node_id, kind=EdgeKind.CONTAINS))
        for param in decl.params:
            self._add_parameter(graph, node_id, qual, rel_path, param.name, param.node)

    def _add_parameter(
        self,
        graph: RepoGraph,
        func_id: str,
        func_qual: str,
        rel_path: str,
        name: str,
        ts_node: TSNode,
    ) -> None:
        param_id = f"param:{func_qual}.{name}"
        graph.add_node(
            Node(
                id=param_id,
                kind=NodeKind.Parameter,
                name=name,
                path=rel_path,
                start_line=ts_node.start_point[0] + 1,
                end_line=ts_node.end_point[0] + 1,
            )
        )
        graph.add_edge(Edge(src=func_id, dst=param_id, kind=EdgeKind.CONTAINS))

    def _add_class(
        self,
        graph: RepoGraph,
        parent_id: str,
        parent_qual: str,
        rel_path: str,
        decl: ClassDecl,
    ) -> None:
        qual = f"{parent_qual}.{decl.name}"
        node_id = f"class:{qual}"
        graph.add_node(
            Node(
                id=node_id,
                kind=NodeKind.Class,
                name=decl.name,
                path=rel_path,
                start_line=decl.node.start_point[0] + 1,
                end_line=decl.node.end_point[0] + 1,
                attrs={"qualname": qual, "fields": dict(decl.fields)},
            )
        )
        graph.add_edge(Edge(src=parent_id, dst=node_id, kind=EdgeKind.CONTAINS))
        for method in decl.methods:
            self._add_function(graph, node_id, qual, rel_path, method, is_method=True)

    def _add_import(
        self, graph: RepoGraph, module_id: str, rel_path: str, decl: ImportDecl
    ) -> None:
        import_id = f"import:{module_id}::{decl.target}"
        graph.add_node(
            Node(
                id=import_id,
                kind=NodeKind.Import,
                name=decl.target,
                path=rel_path,
                start_line=decl.node.start_point[0] + 1,
                end_line=decl.node.end_point[0] + 1,
                attrs={"target": decl.target, "alias": decl.alias},
            )
        )
        graph.add_edge(Edge(src=module_id, dst=import_id, kind=EdgeKind.CONTAINS))
        graph.add_edge(
            Edge(src=module_id, dst=import_id, kind=EdgeKind.IMPORTS, attrs={"target": decl.target})
        )

    def _add_variable(
        self, graph: RepoGraph, module_id: str, module_name: str, rel_path: str, decl: VariableDecl
    ) -> None:
        var_id = f"var:{module_name}.{decl.name}"
        graph.add_node(
            Node(
                id=var_id,
                kind=NodeKind.Variable,
                name=decl.name,
                path=rel_path,
                start_line=decl.node.start_point[0] + 1,
                end_line=decl.node.end_point[0] + 1,
                attrs={"qualname": f"{module_name}.{decl.name}"},
            )
        )
        graph.add_edge(Edge(src=module_id, dst=var_id, kind=EdgeKind.CONTAINS))


__all__ = ["DEFAULT_IGNORE_DIRS", "TreeSitterSource"]
