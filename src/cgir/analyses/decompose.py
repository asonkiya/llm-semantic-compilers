"""``cgir decompose`` — suggest functional-core / imperative-shell splits.

Rung 2 of the rewrite vision (docs/vision-rewrite.md): for an impure
function, find the **pure computational core** — statements that neither
perform effects nor depend (data- or control-wise) on effect results — and
*suggest* extracting it. Advisory only: no code is rewritten; the safety
net for acting on a suggestion is extract → pin ``pure`` → ``cgir verify``.

Method (all existing machinery):
1. Per-statement effects — each CFG statement's tree-sitter subtree is
   classified via the adapter's ``classify_calls``; call sites resolving to
   impure in-repo callees (via existing CALLS edges) also mark their
   statement effectful.
2. Shell closure over the PDG — from effectful statements, follow
   ``FLOWS_TO`` forward (effect-derived data taints consumers) and pull in
   everything controlled by a tainted/effectful controller. A control
   region containing an effect is collapsed whole (you can't extract half
   a loop).
3. Core = the remaining statements. Inputs are names the core reads but
   doesn't write; outputs are core-written names the shell (or a shell
   return) consumes.

Honest limits: suggestion quality tracks each adapter's CFG read/write
fidelity (Python sharpest); no aliasing analysis; statement order isn't
modelled beyond the PDG, so a suggestion can interleave — treat it as a
worklist, not a patch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cgir.analyses.effects import (
    IMPURE_EFFECT_TAGS,
    TRANSITIVE_TAG,
    _module_alias_maps,
)
from cgir.analyses.symbols import module_of
from cgir.ir.edges import EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import NodeKind
from cgir.languages.cache import SourceCache

_STATEMENT_KINDS = {
    NodeKind.Statement,
    NodeKind.Assignment,
    NodeKind.Return,
    NodeKind.Branch,
    NodeKind.Loop,
}
_COUNTED_KINDS = {NodeKind.Statement, NodeKind.Assignment, NodeKind.Return}


@dataclass(slots=True)
class DecomposeResult:
    function_id: str
    decomposable: bool
    reason: str = ""
    core_statements: int = 0
    total_statements: int = 0
    core_lines: list[tuple[int, int]] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    shell_effects: list[str] = field(default_factory=list)


def decompose(
    graph: RepoGraph,
    effects: dict[str, list[str]],
    function_id: str,
    repo_path: Path,
    min_core: int = 3,
) -> DecomposeResult:
    func = graph.get_node(function_id)
    tags = set(effects.get(function_id, []))
    if not tags & (IMPURE_EFFECT_TAGS | {TRANSITIVE_TAG}):
        return DecomposeResult(function_id, False, reason="already pure")

    cfg_nodes = [c for c in graph.children(function_id) if c.kind in _STATEMENT_KINDS]
    if not cfg_nodes:
        return DecomposeResult(function_id, False, reason="no CFG (unsupported or empty)")

    cache = SourceCache(repo_path)
    func_ts = cache.locate(func.path, func.name, (func.start_line or 1) - 1) if func.path else None
    if func_ts is None:
        return DecomposeResult(function_id, False, reason="source not locatable")
    parsed = cache.get(func.path or "")
    assert parsed is not None
    source, _root, adapter = parsed
    module_id = module_of(graph, func)
    aliases = _module_alias_maps(graph).get(module_id or "", {})

    # subtree per statement, keyed by exact (start_row, end_row) span —
    # a body block shares its start row with its first statement, so start
    # row alone would leak whole-body effects onto that statement
    by_span: dict[tuple[int, int], Any] = {}
    stack = [func_ts]
    while stack:
        node = stack.pop()
        by_span.setdefault((node.start_point[0], node.end_point[0]), node)
        stack.extend(reversed(node.children))

    # lines of calls to impure in-repo callees
    impure_callee_lines: dict[int, str] = {}
    impure_callees = {
        graph.get_node(e.dst).name
        for e in graph.out_edges(function_id, EdgeKind.CALLS)
        if set(effects.get(e.dst, [])) & (IMPURE_EFFECT_TAGS | {TRANSITIVE_TAG})
    }
    if impure_callees:
        for callee, _args, line in adapter.call_sites(func_ts, source):
            leaf = callee.rsplit(".", 1)[-1]
            if leaf in impure_callees:
                impure_callee_lines[line] = leaf

    effectful: set[str] = set()
    shell_effects: list[str] = []
    for cfg in cfg_nodes:
        if cfg.start_line is None or cfg.end_line is None:
            continue
        subtree = by_span.get((cfg.start_line - 1, cfg.end_line - 1))
        stmt_tags: set[str] = set()
        if subtree is not None:
            stmt_tags = set(adapter.classify_calls(subtree, source, aliases))
        called = [
            name
            for line, name in impure_callee_lines.items()
            if cfg.start_line <= line <= cfg.end_line
        ]
        impure_here = (stmt_tags & IMPURE_EFFECT_TAGS) or called
        if impure_here:
            effectful.add(cfg.id)
            for tag in sorted(stmt_tags & IMPURE_EFFECT_TAGS):
                shell_effects.append(f"line {cfg.start_line}: {tag}")
            for name in called:
                shell_effects.append(f"line {cfg.start_line}: calls {name} (impure)")

    if not effectful:
        return DecomposeResult(
            function_id, False, reason="effects not locatable at statement level"
        )

    # shell closure: FLOWS_TO forward; whole control regions containing effects
    ids = {c.id for c in cfg_nodes}
    shell = set(effectful)
    changed = True
    while changed:
        changed = False
        for node_id in list(shell):
            for e in graph.out_edges(node_id, EdgeKind.FLOWS_TO):
                if e.dst in ids and e.dst not in shell:
                    shell.add(e.dst)
                    changed = True
        for cfg in cfg_nodes:
            if cfg.id in shell:
                for e in graph.out_edges(cfg.id, EdgeKind.DEPENDS_ON):
                    # controller of an effectful/tainted stmt joins the shell
                    if e.dst in ids and e.dst not in shell:
                        shell.add(e.dst)
                        changed = True
            else:
                # anything controlled by a shell controller joins the shell
                for e in graph.out_edges(cfg.id, EdgeKind.DEPENDS_ON):
                    if e.dst in shell:
                        shell.add(cfg.id)
                        changed = True
                        break

    core = [c for c in cfg_nodes if c.id not in shell]
    core_counted = [c for c in core if c.kind in _COUNTED_KINDS]
    total_counted = [c for c in cfg_nodes if c.kind in _COUNTED_KINDS]
    if len(core_counted) < min_core:
        return DecomposeResult(
            function_id,
            False,
            reason=f"core too small ({len(core_counted)} < {min_core})",
            core_statements=len(core_counted),
            total_statements=len(total_counted),
            shell_effects=sorted(set(shell_effects)),
        )

    core_writes: set[str] = set()
    core_reads: set[str] = set()
    for c in core:
        attrs = c.attrs or {}
        core_writes.update(attrs.get("writes") or [])
        core_reads.update(attrs.get("reads") or [])
    shell_reads: set[str] = set()
    for c in cfg_nodes:
        if c.id in shell:
            shell_reads.update((c.attrs or {}).get("reads") or [])

    inputs = sorted(core_reads - core_writes)
    outputs = sorted(core_writes & shell_reads)

    lines = sorted(
        (c.start_line, c.end_line)
        for c in core
        if c.start_line is not None and c.end_line is not None
    )
    return DecomposeResult(
        function_id,
        True,
        core_statements=len(core_counted),
        total_statements=len(total_counted),
        core_lines=lines,
        inputs=inputs,
        outputs=outputs,
        shell_effects=sorted(set(shell_effects)),
    )


def decompose_all(
    graph: RepoGraph,
    effects: dict[str, list[str]],
    repo_path: Path,
    min_core: int = 3,
) -> dict[str, Any]:
    """Repo-wide decomposability report — the rung-2 metric."""
    results: list[DecomposeResult] = []
    for node in graph.nodes():
        if node.kind not in {NodeKind.Function, NodeKind.Method}:
            continue
        tags = set(effects.get(node.id, []))
        if not tags & (IMPURE_EFFECT_TAGS | {TRANSITIVE_TAG}):
            continue
        results.append(decompose(graph, effects, node.id, repo_path, min_core=min_core))
    decomposable = [r for r in results if r.decomposable]
    return {
        "impure_functions": len(results),
        "decomposable": len(decomposable),
        "decomposability_pct": round(100 * len(decomposable) / len(results)) if results else 0,
        "results": results,
    }


def render_decompose(result: DecomposeResult) -> str:
    lines = [f"# decompose {result.function_id}", ""]
    if not result.decomposable:
        lines.append(f"decomposable: no — {result.reason}")
        if result.shell_effects:
            lines.append("effects found:")
            lines.extend(f"  {s}" for s in result.shell_effects)
        return "\n".join(lines) + "\n"
    lines.append(
        f"decomposable: yes ({result.core_statements} of {result.total_statements} "
        "statements form a pure core)"
    )
    lines.append("")
    sig_in = ", ".join(result.inputs) or "(none)"
    sig_out = ", ".join(result.outputs) or "(return value)"
    lines.append(f"  proposed core inputs:  {sig_in}")
    lines.append(f"  proposed core outputs: {sig_out}")
    spans = ", ".join(f"{s}-{e}" if s != e else str(s) for s, e in result.core_lines)
    lines.append(f"  core statement lines:  {spans}")
    lines.append("  shell (stays behind):")
    lines.extend(f"    {s}" for s in result.shell_effects)
    lines.append("")
    lines.append("  next: extract core -> pin `cgir: pure` -> cgir verify")
    return "\n".join(lines) + "\n"


__all__ = ["DecomposeResult", "decompose", "decompose_all", "render_decompose"]
