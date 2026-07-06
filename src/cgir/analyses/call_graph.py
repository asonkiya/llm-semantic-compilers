"""Build CALLS edges from resolved symbol tables.

Language-neutral: the active :class:`~cgir.languages.LanguageAdapter`
supplies each function's call sites (dotted callee, arg names, line); this
module resolves each callee through the owning module's symbol table and
emits the edge. Unresolved calls are dropped — third-party effects show up
via :mod:`cgir.analyses.effects` instead.
"""

from __future__ import annotations

from pathlib import Path

from cgir.analyses.symbols import SymbolTable, module_of
from cgir.ir.edges import Edge, EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import Node, NodeKind
from cgir.languages import LanguageAdapter, SourceCache

_SELF_RECEIVERS: frozenset[str] = frozenset({"this", "self"})


def build_call_graph(
    graph: RepoGraph,
    tables: dict[str, SymbolTable],
    repo_path: Path,
    adapter: LanguageAdapter | None = None,
) -> None:
    cache = SourceCache(repo_path, adapter)
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
        source, root, file_adapter = parsed
        func_ts = file_adapter.locate_function(root, func.name, (func.start_line or 1) - 1)
        if func_ts is None:
            continue
        class_fields = _owning_class_fields(graph, func)
        for callee_name, arg_names, line in file_adapter.call_sites(func_ts, source):
            target = _resolve_callee(tables, table, callee_name)
            if target is None and class_fields:
                target = _resolve_field_call(graph, table, class_fields, callee_name)
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


def _owning_class_fields(graph: RepoGraph, func: Node) -> dict[str, str]:
    """The field→type map of the class owning a method (empty for free functions)."""
    for edge in graph.in_edges(func.id, EdgeKind.CONTAINS):
        parent = graph.get_node(edge.src)
        if parent.kind == NodeKind.Class:
            fields = parent.attrs.get("fields")
            return fields if isinstance(fields, dict) else {}
    return {}


def _resolve_field_call(
    graph: RepoGraph, table: SymbolTable, class_fields: dict[str, str], dotted: str
) -> str | None:
    """Resolve ``this.<field>.<method>`` via the field's declared type.

    ``this.svc.translate`` where ``svc: ChaptersService`` resolves to the
    method on the class that ``ChaptersService`` binds to (DI / receiver
    calls). Requires the type to resolve to an in-repo Class node.
    """
    parts = dotted.split(".")
    if len(parts) < 3 or parts[0] not in _SELF_RECEIVERS:
        return None
    type_name = class_fields.get(parts[1])
    if type_name is None:
        return None
    binding = table.bindings.get(type_name)
    if binding is None or not binding.startswith("class:"):
        return None
    method_id = f"method:{binding[len('class:') :]}.{parts[2]}"
    return method_id if graph.has_node(method_id) else None


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
