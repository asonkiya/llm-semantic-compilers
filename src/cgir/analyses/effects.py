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
from cgir.languages import LanguageAdapter, SourceCache

DIRECT_EFFECT_TAGS: frozenset[str] = frozenset({"io", "raise", "net", "fs", "nondeterm", "db"})
IMPURE_EFFECT_TAGS: frozenset[str] = DIRECT_EFFECT_TAGS - {"raise"}
TRANSITIVE_TAG = "calls_effectful"


def classify(
    graph: RepoGraph, repo_path: Path, adapter: LanguageAdapter | None = None
) -> dict[str, list[str]]:
    """Return ``{function_id: sorted([effect_tag, ...])}`` for every function/method."""
    return classify_with_confidence(graph, repo_path, adapter)[0]


def classify_with_confidence(
    graph: RepoGraph, repo_path: Path, adapter: LanguageAdapter | None = None
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """``(effects, lexical_effects)`` — the second maps each function to the
    subset of its tags backed only by *lexical* evidence (suffix / receiver-
    name heuristics), the measured false-positive class. Gate rules treat
    those as low-confidence by default.

    ``calls_effectful`` confidence follows the callees: it is high when any
    reachable callee carries a high-confidence impure tag, lexical when the
    entire reachable impurity is lexical.
    """
    cache = SourceCache(repo_path, adapter)
    func_nodes = [n for n in graph.nodes() if n.kind in {NodeKind.Function, NodeKind.Method}]
    alias_maps = _module_alias_maps(graph)

    conf: dict[str, dict[str, str]] = {}
    for func in func_nodes:
        module_id = module_of(graph, func)
        aliases = alias_maps.get(module_id, {}) if module_id else {}
        conf[func.id] = _direct_effects_confidence(cache, func, aliases)

    return transitive_close(graph, conf)


def transitive_close(
    graph: RepoGraph, conf: dict[str, dict[str, str]]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Global calls_effectful closure over per-function direct-effect
    confidences. Public so incremental verify can merge old direct effects
    with a changed file's fresh ones and re-close over the merged graph."""
    func_nodes = [n for n in graph.nodes() if n.kind in {NodeKind.Function, NodeKind.Method}]
    # Two fixpoints over CALLS: does any reachable callee carry impurity at
    # all, and is any of it high-confidence? Only *impure* effects taint
    # callers — raise-only callees don't.
    reaches_any: set[str] = set()
    reaches_high: set[str] = set()
    changed = True
    while changed:
        changed = False
        for func in func_nodes:
            for edge in graph.out_edges(func.id, EdgeKind.CALLS):
                callee = conf.get(edge.dst, {})
                impure = {t for t in callee if t in IMPURE_EFFECT_TAGS}
                callee_any = bool(impure) or edge.dst in reaches_any
                callee_high = any(callee[t] == "high" for t in impure) or edge.dst in reaches_high
                if callee_any and func.id not in reaches_any:
                    reaches_any.add(func.id)
                    changed = True
                if callee_high and func.id not in reaches_high:
                    reaches_high.add(func.id)
                    changed = True

    effects: dict[str, list[str]] = {}
    lexical: dict[str, list[str]] = {}
    for func in func_nodes:
        tags = dict(conf.get(func.id, {}))
        if func.id in reaches_any:
            tags[TRANSITIVE_TAG] = "high" if func.id in reaches_high else "lexical"
        effects[func.id] = sorted(tags)
        low = sorted(t for t, c in tags.items() if c == "lexical")
        if low:
            lexical[func.id] = low
    return effects, lexical


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


def _direct_effects_confidence(
    cache: SourceCache, func: Node, aliases: dict[str, str]
) -> dict[str, str]:
    if func.path is None or func.start_line is None:
        return {}
    parsed = cache.get(func.path)
    if parsed is None:
        return {}
    source, _root, adapter = parsed
    func_ts = cache.locate(func.path, func.name, func.start_line - 1)
    if func_ts is None:
        return {}
    return adapter.direct_effects_confidence(func_ts, source, aliases)
