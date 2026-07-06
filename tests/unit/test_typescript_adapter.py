"""End-to-end tests for the TypeScript adapter (Phase 4).

Runs the full language-neutral pipeline over a TS fixture and asserts the
same ComponentSpec contract Python produces: methods, effects, calls,
signatures, and cross-file resolution via relative imports.
"""

from __future__ import annotations

from pathlib import Path

from cgir.analyses.call_graph import build_call_graph
from cgir.analyses.cfg import build as build_cfg
from cgir.analyses.effects import classify
from cgir.analyses.purity import score
from cgir.analyses.symbols import build_symbol_tables
from cgir.ir.component_spec import ComponentKind
from cgir.ir.edges import EdgeKind
from cgir.ir.nodes import NodeKind
from cgir.languages import TypeScriptAdapter, adapter_for_extension
from cgir.slicing import slice_components
from cgir.sources import TreeSitterSource

FIXTURE = Path(__file__).parent.parent / "fixtures" / "ts_sample"


def _specs() -> dict:
    graph = TreeSitterSource().ingest(FIXTURE)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, FIXTURE)
    build_cfg(graph, FIXTURE)
    effects = classify(graph, FIXTURE)
    purity = score(graph, effects)
    return {s.id: s for s in slice_components(graph, effects=effects, purity_scores=purity)}


def test_typescript_registered_for_ts_extension() -> None:
    assert adapter_for_extension(".ts") is not None
    assert adapter_for_extension(".ts").name == "typescript"
    assert adapter_for_extension(".tsx").name == "typescript"


def test_methods_and_functions_ingested() -> None:
    specs = _specs()
    assert "api.service.NovelService.get" in specs
    assert "api.service.NovelService.label" in specs
    assert "util.format" in specs


def test_signature_and_params_extracted() -> None:
    specs = _specs()
    get = specs["api.service.NovelService.get"]
    assert get.inputs == ["id"]
    assert "id: number" in get.signature


def test_http_call_is_net_effect() -> None:
    specs = _specs()
    assert "net" in specs["api.service.NovelService.get"].effects
    assert specs["api.service.NovelService.get"].kind == ComponentKind.effect_adapter


def test_pure_helper_stays_pure() -> None:
    specs = _specs()
    assert specs["util.format"].kind == ComponentKind.pure_function


def test_relative_import_drives_cross_file_call() -> None:
    """`import { format } from "../util"` resolves the call cross-file."""
    graph = TreeSitterSource().ingest(FIXTURE)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, FIXTURE)
    label_id = "method:api.service.NovelService.label"
    callees = {e.dst for e in graph.out_edges(label_id, EdgeKind.CALLS)}
    assert "func:util.format" in callees


def test_cfg_built_for_typescript() -> None:
    """The generic CFG builder runs on TS branches (util.format has an if)."""
    graph = TreeSitterSource().ingest(FIXTURE)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, FIXTURE)
    build_cfg(graph, FIXTURE)
    branches = [
        c for c in graph.children("func:util.format") if c.kind == NodeKind.Branch
    ]
    assert branches, "expected a Branch node from the if-statement"


def test_adapter_direct_effects_unit() -> None:
    a = TypeScriptAdapter()
    root = a.parse(b"function f(db){ return db.query('x'); }")
    fn = a.locate_function(root, "f", 0)
    assert fn is not None
    assert "db" in a.direct_effects(fn, b"function f(db){ return db.query('x'); }", {})
