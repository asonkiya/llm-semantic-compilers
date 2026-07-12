"""End-to-end tests for the Go adapter — the third language on the seam.

Mirrors the TypeScript suite: the full language-neutral pipeline over Go
fixtures, asserting the same ComponentSpec contract. Go-mapping decisions:
package = directory (same-package cross-file calls resolve via a directory
merge in the symbol tables); struct/interface types → Class nodes with
fields (composition maps onto the DI machinery); ``raise`` ≙ ``panic(``
(Go errors are values, not effects).
"""

from __future__ import annotations

from pathlib import Path

from cgir.analyses.call_graph import build_call_graph
from cgir.analyses.cfg import build as build_cfg
from cgir.analyses.effects import classify
from cgir.analyses.purity import score
from cgir.analyses.symbols import build_symbol_tables
from cgir.ir.component_spec import ComponentKind, ComponentSpec
from cgir.ir.edges import EdgeKind
from cgir.ir.nodes import NodeKind
from cgir.languages import adapter_for_extension
from cgir.slicing import slice_components
from cgir.sources import TreeSitterSource

SERVICE = """\
package store

import (
\t"fmt"
\t"net/http"
\t"time"
)

type Client struct {
\tBase string
}

func (c *Client) Fetch(id int) error {
\t_, err := http.Get(c.Base)
\treturn err
}

type Store struct {
\tclient Client
\tName   string
}

func (s *Store) Sync(id int) error {
\treturn s.client.Fetch(id)
}

func Add(a int, b int) int {
\tif a > b {
\t\treturn a + b
\t}
\tfor i := 0; i < b; i++ {
\t\ta++
\t}
\treturn a
}

func Log(msg string) {
\tfmt.Println(msg)
}

func Stamp() int64 {
\treturn time.Now().Unix()
}

func Explode() {
\tpanic("boom")
}
"""


def _scan(tmp_path: Path) -> dict[str, ComponentSpec]:
    graph = TreeSitterSource().ingest(tmp_path)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, tmp_path)
    build_cfg(graph, tmp_path)
    effects = classify(graph, tmp_path)
    purity = score(graph, effects)
    return {s.id: s for s in slice_components(graph, effects=effects, purity_scores=purity)}


def test_go_registered_for_extension() -> None:
    adapter = adapter_for_extension(".go")
    assert adapter is not None and adapter.name == "go"


def test_functions_and_methods_ingested(tmp_path: Path) -> None:
    (tmp_path / "svc.go").write_text(SERVICE)
    specs = _scan(tmp_path)
    assert "svc.Add" in specs
    assert "svc.Client.Fetch" in specs
    assert "svc.Store.Sync" in specs


def test_signature_and_params(tmp_path: Path) -> None:
    (tmp_path / "svc.go").write_text(SERVICE)
    specs = _scan(tmp_path)
    add = specs["svc.Add"]
    assert add.inputs == ["a", "b"]
    assert "a int" in (add.signature or "")
    assert add.language == "go"


def test_effect_detection(tmp_path: Path) -> None:
    (tmp_path / "svc.go").write_text(SERVICE)
    specs = _scan(tmp_path)
    assert "net" in specs["svc.Client.Fetch"].effects
    assert "io" in specs["svc.Log"].effects
    assert "nondeterm" in specs["svc.Stamp"].effects
    assert "raise" in specs["svc.Explode"].effects
    # panic alone is not impure (raise taxonomy) — but net is
    assert specs["svc.Client.Fetch"].kind == ComponentKind.effect_adapter


def test_pure_function_stays_pure(tmp_path: Path) -> None:
    (tmp_path / "svc.go").write_text(SERVICE)
    specs = _scan(tmp_path)
    assert specs["svc.Add"].kind == ComponentKind.pure_function
    assert specs["svc.Add"].purity == 1.0


def test_struct_fields_extracted(tmp_path: Path) -> None:
    (tmp_path / "svc.go").write_text(SERVICE)
    graph = TreeSitterSource().ingest(tmp_path)
    store = next(n for n in graph.nodes(NodeKind.Class) if n.name == "Store")
    fields = store.attrs.get("fields") or {}
    assert fields.get("client") == "Client"
    assert fields.get("Name") == "string"


def test_receiver_field_call_resolves(tmp_path: Path) -> None:
    """s.client.Fetch(id) resolves via the struct field's type — Go DI."""
    (tmp_path / "svc.go").write_text(SERVICE)
    graph = TreeSitterSource().ingest(tmp_path)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, tmp_path)
    callees = {e.dst for e in graph.out_edges("method:svc.Store.Sync", EdgeKind.CALLS)}
    assert "method:svc.Client.Fetch" in callees


def test_receiver_field_call_taints_caller(tmp_path: Path) -> None:
    (tmp_path / "svc.go").write_text(SERVICE)
    specs = _scan(tmp_path)
    assert "calls_effectful" in specs["svc.Store.Sync"].effects


def test_same_package_cross_file_call_resolves(tmp_path: Path) -> None:
    """Go files in one directory share a package — no import needed."""
    (tmp_path / "a.go").write_text(
        'package p\n\nimport "fmt"\n\nfunc Emit(x int) {\n\tfmt.Println(x)\n}\n'
    )
    (tmp_path / "b.go").write_text("package p\n\nfunc Run(x int) {\n\tEmit(x)\n}\n")
    graph = TreeSitterSource().ingest(tmp_path)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, tmp_path)
    callees = {e.dst for e in graph.out_edges("func:b.Run", EdgeKind.CALLS)}
    assert "func:a.Emit" in callees


def test_cfg_branch_and_loop(tmp_path: Path) -> None:
    (tmp_path / "svc.go").write_text(SERVICE)
    graph = TreeSitterSource().ingest(tmp_path)
    build_cfg(graph, tmp_path)
    kinds = {c.kind for c in graph.children("func:svc.Add")}
    assert NodeKind.Branch in kinds
    assert NodeKind.Loop in kinds


def test_db_receiver_effect(tmp_path: Path) -> None:
    (tmp_path / "q.go").write_text(
        "package q\n\nfunc Load(db DB, id int) error {\n"
        '\t_, err := db.Query("SELECT 1", id)\n\treturn err\n}\n'
    )
    specs = _scan(tmp_path)
    assert "db" in specs["q.Load"].effects


def test_go_pins_extracted(tmp_path: Path) -> None:
    (tmp_path / "p.go").write_text(
        "package p\n\n// cgir: pure\nfunc Add(a int, b int) int {\n\treturn a + b\n}\n"
    )
    specs = _scan(tmp_path)
    assert specs["p.Add"].pins == ["pure"]


def _cross_pkg_repo(tmp_path: Path, gomod: bool = True) -> Path:
    if gomod:
        (tmp_path / "go.mod").write_text("module example.com/myapp\n\ngo 1.22\n")
    store = tmp_path / "internal" / "store"
    store.mkdir(parents=True)
    (store / "keys.go").write_text(
        'package store\n\nimport "fmt"\n\nfunc SaveKey(k string) {\n\tfmt.Println(k)\n}\n'
    )
    api = tmp_path / "api"
    api.mkdir()
    (api / "handler.go").write_text(
        "package api\n\n"
        'import "example.com/myapp/internal/store"\n\n'
        "func Handle(k string) {\n\tstore.SaveKey(k)\n}\n"
    )
    return tmp_path


def test_cross_package_call_resolves_via_gomod(tmp_path: Path) -> None:
    """import "example.com/myapp/internal/store" resolves via the go.mod
    module directive: strip the prefix, bind to the package directory."""
    repo = _cross_pkg_repo(tmp_path)
    graph = TreeSitterSource().ingest(repo)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, repo)
    callees = {e.dst for e in graph.out_edges("func:api.handler.Handle", EdgeKind.CALLS)}
    assert "func:internal.store.keys.SaveKey" in callees


def test_cross_package_taints_caller(tmp_path: Path) -> None:
    repo = _cross_pkg_repo(tmp_path)
    specs = _scan(repo)
    assert "calls_effectful" in specs["api.handler.Handle"].effects


def test_cross_package_without_gomod_falls_back_to_suffix(tmp_path: Path) -> None:
    repo = _cross_pkg_repo(tmp_path, gomod=False)
    graph = TreeSitterSource().ingest(repo)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, repo)
    callees = {e.dst for e in graph.out_edges("func:api.handler.Handle", EdgeKind.CALLS)}
    assert "func:internal.store.keys.SaveKey" in callees
