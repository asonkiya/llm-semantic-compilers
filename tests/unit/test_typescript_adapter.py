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
    branches = [c for c in graph.children("func:util.format") if c.kind == NodeKind.Branch]
    assert branches, "expected a Branch node from the if-statement"


def test_di_field_types_extracted(tmp_path: Path) -> None:
    """Constructor-injected service fields are recorded as class field types."""
    (tmp_path / "cmp.ts").write_text(
        "class Reader {\n"
        "  private base = '/api';\n"
        "  constructor(private svc: ChaptersService, plain: number) {}\n"
        "  go() { return this.svc.translate(1); }\n"
        "}\n"
    )
    graph = TreeSitterSource().ingest(tmp_path)
    cls = next(n for n in graph.nodes(NodeKind.Class) if n.name == "Reader")
    fields = cls.attrs.get("fields")
    assert fields is not None
    assert fields.get("svc") == "ChaptersService"
    assert "plain" not in fields  # a plain (non-field) param


def test_di_cross_service_call_resolves(tmp_path: Path) -> None:
    """`this.svc.method()` resolves to the injected service's method (CALLS)."""
    (tmp_path / "chapters.service.ts").write_text(
        "export class ChaptersService {\n"
        "  constructor(private http: HttpClient) {}\n"
        "  translate(id: number) { return this.http.post(`/x/${id}`, {}); }\n"
        "}\n"
    )
    (tmp_path / "reader.component.ts").write_text(
        "import { ChaptersService } from './chapters.service';\n"
        "export class ReaderComponent {\n"
        "  constructor(private svc: ChaptersService) {}\n"
        "  run() { return this.svc.translate(1); }\n"
        "}\n"
    )
    graph = TreeSitterSource().ingest(tmp_path)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, tmp_path)
    run_id = "method:reader.component.ReaderComponent.run"
    callees = {e.dst for e in graph.out_edges(run_id, EdgeKind.CALLS)}
    assert "method:chapters.service.ChaptersService.translate" in callees


def test_di_call_makes_caller_effectful(tmp_path: Path) -> None:
    """Once the service call resolves, the caller inherits calls_effectful."""
    (tmp_path / "chapters.service.ts").write_text(
        "export class ChaptersService {\n"
        "  constructor(private http: HttpClient) {}\n"
        "  translate(id: number) { return this.http.post(`/x/${id}`, {}); }\n"
        "}\n"
    )
    (tmp_path / "reader.component.ts").write_text(
        "import { ChaptersService } from './chapters.service';\n"
        "export class ReaderComponent {\n"
        "  constructor(private svc: ChaptersService) {}\n"
        "  run() { return this.svc.translate(1); }\n"
        "}\n"
    )
    graph = TreeSitterSource().ingest(tmp_path)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, tmp_path)
    effects = classify(graph, tmp_path)
    assert "calls_effectful" in effects["method:reader.component.ReaderComponent.run"]


def test_pack_names_di_receiver_field(tmp_path: Path) -> None:
    """The pack renders a DI callee as `this.<field>.method(...)`, not bare.

    Without the receiver field name a rewriting model hallucinates it
    (`this.chaptersService` vs the real `this.svc`), the call fails to
    resolve, and the effect contract silently drops.
    """
    from cgir.cli import _call_receivers
    from cgir.report.pack import build_pack, render_pack

    (tmp_path / "chapters.service.ts").write_text(
        "export class ChaptersService {\n"
        "  constructor(private http: HttpClient) {}\n"
        "  translate(id: number) { return this.http.post(`/x/${id}`, {}); }\n"
        "}\n"
    )
    (tmp_path / "reader.component.ts").write_text(
        "import { ChaptersService } from './chapters.service';\n"
        "export class ReaderComponent {\n"
        "  constructor(private svc: ChaptersService) {}\n"
        "  run() { return this.svc.translate(1); }\n"
        "}\n"
    )
    graph = TreeSitterSource().ingest(tmp_path)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, tmp_path)
    effects = classify(graph, tmp_path)
    purity = score(graph, effects)
    specs = list(slice_components(graph, effects=effects, purity_scores=purity))
    target = next(s for s in specs if s.id.endswith("ReaderComponent.run"))

    receivers = _call_receivers(graph, target)
    assert any(v == "this.svc" for v in receivers.values())

    rendered = render_pack(build_pack(specs, target.id, receivers=receivers))
    assert "this.svc.translate" in rendered


def test_adapter_direct_effects_unit() -> None:
    a = TypeScriptAdapter()
    root = a.parse(b"function f(db){ return db.query('x'); }")
    fn = a.locate_function(root, "f", 0)
    assert fn is not None
    assert "db" in a.direct_effects(fn, b"function f(db){ return db.query('x'); }", {})
