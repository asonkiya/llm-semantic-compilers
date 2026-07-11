"""Per-module symbol tables + cross-file import resolution for Python.

Resolution rules:
* ``import x.y.z`` binds the local name ``x`` to module ``x.y.z`` (Python's
  actual semantics differ, but for call-graph purposes we only care that the
  dotted target resolves).
* ``from a.b import c`` binds the local name ``c`` to the qualified symbol
  ``a.b.c``. If that resolves to a known ``Function``/``Class`` node, we
  record it; otherwise the binding stays opaque (third-party).
* ``import x as y`` / ``from a import b as c`` bind the *alias* (recorded
  by the ingester on the Import node); the original name is not bound.
* Top-level ``def``/``class`` in a module bind their names in that module's
  table.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cgir.ir.edges import EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import Node, NodeKind


@dataclass
class SymbolTable:
    """Local name → graph node id (or ``None`` for opaque external symbols)."""

    module_id: str
    bindings: dict[str, str | None] = field(default_factory=dict)


def build_symbol_tables(graph: RepoGraph) -> dict[str, SymbolTable]:
    qualname_index = _qualname_index(graph)
    tables: dict[str, SymbolTable] = {}

    for module in graph.nodes(NodeKind.Module):
        table = SymbolTable(module_id=module.id)
        for child in graph.children(module.id):
            if child.kind in {NodeKind.Function, NodeKind.Class}:
                table.bindings[child.name] = child.id
        tables[module.id] = table

    _merge_go_packages(graph, tables)

    for module in graph.nodes(NodeKind.Module):
        table = tables[module.id]
        for child in graph.children(module.id, NodeKind.Import):
            target = str(child.attrs.get("target") or child.name)
            alias = child.attrs.get("alias")
            local = alias if isinstance(alias, str) else target.rsplit(".", 1)[-1]
            table.bindings[local] = _resolve_target(qualname_index, target)

    return tables


def _merge_go_packages(graph: RepoGraph, tables: dict[str, SymbolTable]) -> None:
    """Go's package scope: files in one directory share top-level names.

    Modules are per-file, but Go code calls sibling-file functions with no
    import. Union the local bindings of go modules that share a directory
    (existing bindings win, so a file's own names shadow siblings').
    """
    by_dir: dict[str, list[Node]] = {}
    for module in graph.nodes(NodeKind.Module):
        if module.attrs.get("language") != "go" or not module.path:
            continue
        directory = module.path.rsplit("/", 1)[0] if "/" in module.path else ""
        by_dir.setdefault(directory, []).append(module)
    for modules in by_dir.values():
        if len(modules) < 2:
            continue
        union: dict[str, str | None] = {}
        for module in modules:
            union.update(tables[module.id].bindings)
        for module in modules:
            merged = dict(union)
            merged.update(tables[module.id].bindings)  # own names shadow siblings
            tables[module.id].bindings = merged


def _resolve_target(qualname_index: dict[str, str], target: str) -> str | None:
    """Exact qualname match, else a *unique* suffix match.

    The suffix fallback bridges source-root prefixes: a repo whose package
    lives in ``backend/`` (or ``src/``) has module qualnames like
    ``backend.app.repos.chapter`` while its code imports
    ``app.repos.chapter``. Ambiguous suffixes stay unresolved — don't guess.
    """
    hit = qualname_index.get(target)
    if hit is not None:
        return hit
    suffix = "." + target
    candidates = {nid for qual, nid in qualname_index.items() if qual.endswith(suffix)}
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


def _qualname_index(graph: RepoGraph) -> dict[str, str]:
    index: dict[str, str] = {}
    for node in graph.nodes():
        if node.kind not in {NodeKind.Function, NodeKind.Method, NodeKind.Class, NodeKind.Module}:
            continue
        qual = node.attrs.get("qualname") if node.attrs else None
        if isinstance(qual, str):
            index[qual] = node.id
        else:
            index[node.name] = node.id
    return index


def resolve(tables: dict[str, SymbolTable], module_id: str, name: str) -> str | None:
    table = tables.get(module_id)
    if table is None:
        return None
    return table.bindings.get(name)


def module_of(graph: RepoGraph, func_node: Node) -> str | None:
    """Walk CONTAINS edges upward to find the owning Module id."""
    current_id = func_node.id
    visited: set[str] = set()
    while current_id not in visited:
        visited.add(current_id)
        parents = list(graph.in_edges(current_id, EdgeKind.CONTAINS))
        if not parents:
            return None
        parent = graph.get_node(parents[0].src)
        if parent.kind == NodeKind.Module:
            return parent.id
        current_id = parent.id
    return None
