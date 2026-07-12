"""The scan pipeline — single driver shared by the CLI and the HTTP API.

Pipeline order is fixed by the spec:
``ingest → symbols → call_graph → cfg → pdg → effects → purity → slice →
export``. New analyses wire in here (see ``CLAUDE.md`` working
conventions); the CLI and API are thin surfaces over :func:`scan_repo`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from cgir.analyses import coverage_link
from cgir.analyses import effects as effects_pass
from cgir.analyses import purity as purity_pass
from cgir.analyses.call_graph import build_call_graph
from cgir.analyses.cfg import build as build_cfg
from cgir.analyses.pdg import build as build_pdg
from cgir.analyses.symbols import build_symbol_tables
from cgir.config import CGIRConfig
from cgir.export import json_export
from cgir.ir.component_spec import ComponentSpec
from cgir.ir.nodes import NodeKind
from cgir.slicing import slice_components
from cgir.sources import TreeSitterSource
from cgir.trace import build_trace_map


def _coverage_covered(graph: object, repo_path: Path) -> dict[str, set[str]]:
    """Measured test linkage from coverage contexts, when present."""
    from cgir.ir.graph import RepoGraph

    assert isinstance(graph, RepoGraph)
    cov = coverage_link.read_coverage_contexts(repo_path)
    if not cov:
        return {}
    spans = [
        (n.id, n.path, n.start_line, n.end_line)
        for n in graph.nodes()
        if n.kind in {NodeKind.Function, NodeKind.Method}
        and n.path is not None
        and n.start_line is not None
        and n.end_line is not None
    ]
    # coverage tests are named by qualname; span keys are node ids — map back
    qual_by_id = {
        n.id: str(n.attrs.get("qualname") or n.name)
        for n in graph.nodes()
        if n.kind in {NodeKind.Function, NodeKind.Method}
    }
    raw = coverage_link.coverage_covered_by(cov, spans)
    # drop a test covering itself under its own qualname
    return {
        node_id: {t for t in tests if t != qual_by_id.get(node_id)}
        for node_id, tests in raw.items()
    }


@dataclass(slots=True)
class ScanResult:
    out_dir: Path
    specs: list[ComponentSpec]
    # type shapes: qualname -> field name -> type text (for shape-drift)
    types: dict[str, dict[str, str]] = field(default_factory=dict)


def scan_repo(
    repo: Path,
    out: Path | None = None,
    exclude: Iterable[str] | None = None,
) -> ScanResult:
    """Run the full pipeline over ``repo`` and write the index to disk."""
    config = CGIRConfig.for_scan(repo, out)
    source = TreeSitterSource(ignore_dirs=set(exclude or ()))
    graph = source.ingest(config.repo_path)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, config.repo_path)
    build_cfg(graph, config.repo_path)
    build_pdg(graph)
    effects, lexical = effects_pass.classify_with_confidence(graph, config.repo_path)
    purity_scores = purity_pass.score(graph, effects)
    coverage_covered = _coverage_covered(graph, config.repo_path)
    specs = slice_components(
        graph,
        effects=effects,
        purity_scores=purity_scores,
        lexical_effects=lexical,
        coverage_covered=coverage_covered,
    )
    trace_map = build_trace_map(graph)
    json_export.write_index(config.out_dir, graph, specs)
    trace_map.write(config.out_dir / "trace_map.json")
    types = {
        str(node.attrs["qualname"]): dict(node.attrs["fields"])
        for node in graph.nodes(NodeKind.Class)
        if node.attrs.get("fields") and node.attrs.get("qualname")
    }
    return ScanResult(out_dir=config.out_dir, specs=specs, types=types)
