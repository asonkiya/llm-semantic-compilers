"""Side-effect classification — language-neutral algorithm over a LanguageAdapter.

Effect taxonomy (the language-neutral ComponentSpec vocabulary):
    io              terminal / device IO
    raise           contains a raise (informational — see below)
    net             network access
    fs              filesystem access
    nondeterm       randomness / clock reads
    db              database access
    calls_effectful (transitive only) — a callee has an *impure* effect

``raise`` is recorded but **not impure** (settled Sprint 13): exceptions
are control flow / part of the contract, so a raise-only function keeps
purity 1.0 and does not taint callers. :data:`IMPURE_EFFECT_TAGS` is the
gate used by purity, classification, and the transitive closure.

This module owns only the *algorithm*: build per-module import-alias maps
(language-neutral, from ``Import`` node attrs), ask the active
:class:`~cgir.languages.LanguageAdapter` for each function's direct effect
tags, then propagate ``calls_effectful`` transitively over ``CALLS`` to a
fixed point. Which calls count as which effect is the adapter's business
(``PythonAdapter.direct_effects`` and its stdlib tables).
"""

from __future__ import annotations

from pathlib import Path

from cgir.analyses.symbols import module_of
from cgir.ir.edges import EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import Node, NodeKind
from cgir.languages import DEFAULT_ADAPTER, LanguageAdapter, SourceCache

DIRECT_EFFECT_TAGS: frozenset[str] = frozenset({"io", "raise", "net", "fs", "nondeterm", "db"})
IMPURE_EFFECT_TAGS: frozenset[str] = DIRECT_EFFECT_TAGS - {"raise"}
TRANSITIVE_TAG = "calls_effectful"


def classify(
    graph: RepoGraph, repo_path: Path, adapter: LanguageAdapter | None = None
) -> dict[str, list[str]]:
    """Return ``{function_id: sorted([effect_tag, ...])}`` for every function/method."""
    adapter = adapter or DEFAULT_ADAPTER
    cache = SourceCache(adapter, repo_path)
    func_nodes = [n for n in graph.nodes() if n.kind in {NodeKind.Function, NodeKind.Method}]
    alias_maps = _module_alias_maps(graph)

    effects: dict[str, set[str]] = {}
    for func in func_nodes:
        module_id = module_of(graph, func)
        aliases = alias_maps.get(module_id, {}) if module_id else {}
        effects[func.id] = _direct_effects(cache, adapter, func, aliases)

    # Propagate transitively over CALLS edges until fixed point. Only
    # *impure* effects taint callers — raise-only callees don't.
    changed = True
    while changed:
        changed = False
        for func in func_nodes:
            for edge in graph.out_edges(func.id, EdgeKind.CALLS):
                callee = effects.get(edge.dst, set())
                if (
                    callee & IMPURE_EFFECT_TAGS or TRANSITIVE_TAG in callee
                ) and TRANSITIVE_TAG not in effects[func.id]:
                    effects[func.id].add(TRANSITIVE_TAG)
                    changed = True

    return {nid: sorted(tags) for nid, tags in effects.items()}


def _module_alias_maps(graph: RepoGraph) -> dict[str, dict[str, str]]:
    """Per module: ``{local_name: absolute_dotted_target}`` from Import nodes."""
    maps: dict[str, dict[str, str]] = {}
    for module in graph.nodes(NodeKind.Module):
        table: dict[str, str] = {}
        for child in graph.children(module.id, NodeKind.Import):
            target = str(child.attrs.get("target") or child.name)
            alias = child.attrs.get("alias")
            local = alias if isinstance(alias, str) else target.rsplit(".", 1)[-1]
            table[local] = target
        maps[module.id] = table
    return maps


def _direct_effects(
    cache: SourceCache, adapter: LanguageAdapter, func: Node, aliases: dict[str, str]
) -> set[str]:
    if func.path is None or func.start_line is None:
        return set()
    parsed = cache.get(func.path)
    if parsed is None:
        return set()
    source, root = parsed
    func_ts = adapter.locate_function(root, func.name, func.start_line - 1)
    if func_ts is None:
        return set()
    return adapter.direct_effects(func_ts, source, aliases)
