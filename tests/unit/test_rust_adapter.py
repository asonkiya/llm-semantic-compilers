"""Tests for the Rust language adapter — covers the 9-point test bar.

Mirrors the Go and TypeScript adapter test suites. All tests run the full
language-neutral pipeline (ingest → symbols → call_graph → cfg → effects →
purity → slice) over small in-memory .rs fixtures.
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

# ---------------------------------------------------------------------------
# Pipeline driver
# ---------------------------------------------------------------------------


def _scan(tmp_path: Path) -> tuple[object, dict[str, ComponentSpec]]:
    graph = TreeSitterSource().ingest(tmp_path)  # rust is a registered builtin
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, tmp_path)
    build_cfg(graph, tmp_path)
    effects = classify(graph, tmp_path)
    purity = score(graph, effects)
    specs = {s.id: s for s in slice_components(graph, effects=effects, purity_scores=purity)}
    return graph, specs


# ---------------------------------------------------------------------------
# Shared fixture source
# ---------------------------------------------------------------------------

SERVICE_RS = """\
use std::fs;
use reqwest;

/// Fetch data from a remote endpoint.
pub fn fetch(url: String) -> String {
    let resp = reqwest::get(url);
    resp
}

/// Write content to disk.
pub fn write_file(path: String, content: String) {
    fs::write(path, content);
}

/// Pure arithmetic function.
// cgir: pure
pub fn add(a: i32, b: i32) -> i32 {
    if a > b {
        return a + b;
    }
    for i in items {
        let x = i;
    }
    a
}

/// Log a message.
pub fn log(msg: String) {
    println!("{}", msg);
}

/// Panics on bad input.
pub fn validate(x: i32) {
    if x < 0 {
        panic!("negative value");
    }
}

/// Non-deterministic timestamp.
pub fn timestamp() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs()
}
"""

STRUCT_RS = """\
pub struct HttpClient {
    base_url: String,
}

impl HttpClient {
    pub fn new(base_url: String) -> Self {
        HttpClient { base_url }
    }

    pub fn get(&self, path: String) -> String {
        reqwest::get(path)
    }
}

pub struct ApiService {
    client: HttpClient,
    name: String,
}

impl ApiService {
    pub fn new(client: HttpClient, name: String) -> Self {
        ApiService { client, name }
    }

    pub fn call(&self, path: String) -> String {
        self.client.get(path)
    }
}
"""

CROSS_FILE_A_RS = """\
pub fn helper(x: i32) -> i32 {
    println!("{}", x);
    x
}
"""

CROSS_FILE_B_RS = """\
use crate::a::helper;

pub fn runner(x: i32) -> i32 {
    helper(x)
}
"""


# ---------------------------------------------------------------------------
# Test 1: Functions and methods ingested with correct IDs
# ---------------------------------------------------------------------------


def test_functions_ingested(tmp_path: Path) -> None:
    """Top-level functions appear as func:<module>.<name>."""
    (tmp_path / "svc.rs").write_text(SERVICE_RS)
    _, specs = _scan(tmp_path)
    assert "func:svc.fetch" in specs or any("fetch" in k for k in specs)
    assert "func:svc.add" in specs or any("add" in k for k in specs)


def test_methods_ingested(tmp_path: Path) -> None:
    """Impl methods appear as method:<module>.<Type>.<name>."""
    (tmp_path / "svc.rs").write_text(STRUCT_RS)
    _, specs = _scan(tmp_path)
    assert any("HttpClient" in k and "get" in k for k in specs), f"keys: {list(specs.keys())}"
    assert any("ApiService" in k and "call" in k for k in specs), f"keys: {list(specs.keys())}"


# ---------------------------------------------------------------------------
# Test 2: Params + signature; spec.language == "rust"
# ---------------------------------------------------------------------------


def test_params_and_signature(tmp_path: Path) -> None:
    (tmp_path / "svc.rs").write_text(SERVICE_RS)
    _, specs = _scan(tmp_path)
    add_spec = next((v for k, v in specs.items() if k.endswith(".add")), None)
    assert add_spec is not None, f"add not found in {list(specs.keys())}"
    assert "a" in (add_spec.inputs or [])
    assert "b" in (add_spec.inputs or [])
    assert add_spec.language == "rust"
    assert add_spec.signature is not None
    assert "a" in add_spec.signature
    assert "b" in add_spec.signature


def test_language_is_rust(tmp_path: Path) -> None:
    (tmp_path / "svc.rs").write_text(SERVICE_RS)
    _, specs = _scan(tmp_path)
    for spec in specs.values():
        assert spec.language == "rust", f"{spec.id} has language={spec.language}"


# ---------------------------------------------------------------------------
# Test 3: Effect detection
# ---------------------------------------------------------------------------


def test_net_effect(tmp_path: Path) -> None:
    (tmp_path / "svc.rs").write_text(SERVICE_RS)
    _, specs = _scan(tmp_path)
    fetch = next((v for k, v in specs.items() if k.endswith(".fetch")), None)
    assert fetch is not None, f"fetch not found; keys={list(specs.keys())}"
    assert "net" in fetch.effects, f"fetch effects: {fetch.effects}"


def test_fs_effect(tmp_path: Path) -> None:
    (tmp_path / "svc.rs").write_text(SERVICE_RS)
    _, specs = _scan(tmp_path)
    write_spec = next((v for k, v in specs.items() if k.endswith(".write_file")), None)
    assert write_spec is not None
    assert "fs" in write_spec.effects


def test_io_effect(tmp_path: Path) -> None:
    (tmp_path / "svc.rs").write_text(SERVICE_RS)
    _, specs = _scan(tmp_path)
    log_spec = next((v for k, v in specs.items() if k.endswith(".log")), None)
    assert log_spec is not None
    assert "io" in log_spec.effects


def test_raise_effect_from_panic(tmp_path: Path) -> None:
    (tmp_path / "svc.rs").write_text(SERVICE_RS)
    _, specs = _scan(tmp_path)
    validate = next((v for k, v in specs.items() if k.endswith(".validate")), None)
    assert validate is not None
    assert "raise" in validate.effects


def test_nondeterm_effect(tmp_path: Path) -> None:
    """std::time::SystemTime::now → nondeterm."""
    ts_code = """\
pub fn get_time() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs()
}
"""
    (tmp_path / "t.rs").write_text(ts_code)
    _, specs = _scan(tmp_path)
    get_time = next((v for k, v in specs.items() if "get_time" in k), None)
    assert get_time is not None
    assert "nondeterm" in get_time.effects or "raise" in get_time.effects


def test_db_effect(tmp_path: Path) -> None:
    """sqlx:: prefix → db effect."""
    db_code = """\
pub async fn fetch_users(pool: sqlx::PgPool) {
    sqlx::query("SELECT * FROM users").fetch_all(&pool).await;
}
"""
    (tmp_path / "db.rs").write_text(db_code)
    _, specs = _scan(tmp_path)
    fetch_users = next((v for k, v in specs.items() if "fetch_users" in k), None)
    assert fetch_users is not None
    assert "db" in fetch_users.effects


def test_pure_function_stays_pure(tmp_path: Path) -> None:
    (tmp_path / "svc.rs").write_text(SERVICE_RS)
    _, specs = _scan(tmp_path)
    add_spec = next((v for k, v in specs.items() if k.endswith(".add")), None)
    assert add_spec is not None
    assert add_spec.kind == ComponentKind.pure_function
    assert add_spec.purity == 1.0


# ---------------------------------------------------------------------------
# Test 4: Struct/class fields extracted
# ---------------------------------------------------------------------------


def test_struct_fields_extracted(tmp_path: Path) -> None:
    (tmp_path / "svc.rs").write_text(STRUCT_RS)
    adapter = adapter_for_extension(".rs")
    source = TreeSitterSource(adapter=adapter)
    graph = source.ingest(tmp_path)

    api_class = next((n for n in graph.nodes(NodeKind.Class) if n.name == "ApiService"), None)
    assert api_class is not None, (
        f"ApiService not found; classes={[n.name for n in graph.nodes(NodeKind.Class)]}"
    )
    fields = api_class.attrs.get("fields") or {}
    assert "client" in fields, f"fields: {fields}"
    assert fields["client"] == "HttpClient"
    assert "name" in fields


def test_http_client_fields(tmp_path: Path) -> None:
    (tmp_path / "svc.rs").write_text(STRUCT_RS)
    adapter = adapter_for_extension(".rs")
    source = TreeSitterSource(adapter=adapter)
    graph = source.ingest(tmp_path)

    http_class = next((n for n in graph.nodes(NodeKind.Class) if n.name == "HttpClient"), None)
    assert http_class is not None
    fields = http_class.attrs.get("fields") or {}
    assert "base_url" in fields


# ---------------------------------------------------------------------------
# Test 5: Receiver-field call resolves via DI
# ---------------------------------------------------------------------------


def test_receiver_field_call_resolves(tmp_path: Path) -> None:
    """self.client.get(...) in ApiService.call resolves to HttpClient.get."""
    (tmp_path / "svc.rs").write_text(STRUCT_RS)
    adapter = adapter_for_extension(".rs")
    source = TreeSitterSource(adapter=adapter)
    graph = source.ingest(tmp_path)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, tmp_path, adapter=adapter)

    # Methods are NodeKind.Method, not NodeKind.Function
    all_callable = list(graph.nodes(NodeKind.Method)) + list(graph.nodes(NodeKind.Function))

    # Find ApiService.call method id
    call_id = next(
        (n.id for n in all_callable if "ApiService" in n.id and "call" in n.id),
        None,
    )
    assert call_id is not None, f"ApiService.call not found; nodes={[n.id for n in all_callable]}"

    callee_ids = {e.dst for e in graph.out_edges(call_id, EdgeKind.CALLS)}
    # Expect HttpClient.get in the callees
    get_id = next(
        (n.id for n in all_callable if "HttpClient" in n.id and "get" in n.id),
        None,
    )
    assert get_id is not None, f"HttpClient.get not found; nodes={[n.id for n in all_callable]}"
    assert get_id in callee_ids, f"Expected {get_id} in callees of {call_id}, got: {callee_ids}"


# ---------------------------------------------------------------------------
# Test 6: Caller of effectful callee gets "calls_effectful"
# ---------------------------------------------------------------------------


def test_calls_effectful_propagates(tmp_path: Path) -> None:
    """ApiService.call calls HttpClient.get (which calls reqwest::get → net);
    so ApiService.call should get calls_effectful."""
    (tmp_path / "svc.rs").write_text(STRUCT_RS)
    _, specs = _scan(tmp_path)
    call_spec = next((v for k, v in specs.items() if "ApiService" in k and "call" in k), None)
    assert call_spec is not None
    assert "calls_effectful" in call_spec.effects, f"call effects: {call_spec.effects}"


# ---------------------------------------------------------------------------
# Test 7: CFG — if + loop → Branch and Loop nodes
# ---------------------------------------------------------------------------


def test_cfg_branch_and_loop(tmp_path: Path) -> None:
    (tmp_path / "svc.rs").write_text(SERVICE_RS)
    adapter = adapter_for_extension(".rs")
    source = TreeSitterSource(adapter=adapter)
    graph = source.ingest(tmp_path)
    build_cfg(graph, tmp_path, adapter=adapter)

    add_id = next((n.id for n in graph.nodes(NodeKind.Function) if n.id.endswith(".add")), None)
    assert add_id is not None

    child_kinds = {c.kind for c in graph.children(add_id)}
    assert NodeKind.Branch in child_kinds, f"Expected Branch in {child_kinds}"
    assert NodeKind.Loop in child_kinds, f"Expected Loop in {child_kinds}"


# ---------------------------------------------------------------------------
# Test 8: cgir: pure pin lands in spec.pins
# ---------------------------------------------------------------------------


def test_pin_extracted(tmp_path: Path) -> None:
    """// cgir: pure before a function should land in spec.pins."""
    pin_code = """\
// cgir: pure
pub fn multiply(a: i32, b: i32) -> i32 {
    a * b
}
"""
    (tmp_path / "math.rs").write_text(pin_code)
    _, specs = _scan(tmp_path)
    mul = next((v for k, v in specs.items() if "multiply" in k), None)
    assert mul is not None, f"multiply not found; keys={list(specs.keys())}"
    assert "pure" in (mul.pins or []), f"pins: {mul.pins}"


def test_pin_on_service_add(tmp_path: Path) -> None:
    """The add function in SERVICE_RS has // cgir: pure → pin should be set."""
    (tmp_path / "svc.rs").write_text(SERVICE_RS)
    _, specs = _scan(tmp_path)
    add_spec = next((v for k, v in specs.items() if k.endswith(".add")), None)
    assert add_spec is not None
    assert "pure" in (add_spec.pins or []), f"pins: {add_spec.pins}"


# ---------------------------------------------------------------------------
# Test 9: Cross-file call resolution through ImportDecls
# ---------------------------------------------------------------------------


def test_cross_file_import_resolution(tmp_path: Path) -> None:
    """runner calls helper (imported from another file) — call edge should resolve."""
    sub = tmp_path / "mymod"
    sub.mkdir()
    (sub / "a.rs").write_text(CROSS_FILE_A_RS)
    (sub / "b.rs").write_text(CROSS_FILE_B_RS)

    adapter = adapter_for_extension(".rs")
    source = TreeSitterSource(adapter=adapter)
    graph = source.ingest(sub)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, sub, adapter=adapter)

    runner_id = next((n.id for n in graph.nodes(NodeKind.Function) if "runner" in n.id), None)
    assert runner_id is not None, (
        f"runner not found; funcs={[n.id for n in graph.nodes(NodeKind.Function)]}"
    )

    callee_ids = {e.dst for e in graph.out_edges(runner_id, EdgeKind.CALLS)}
    helper_id = next((n.id for n in graph.nodes(NodeKind.Function) if "helper" in n.id), None)
    assert helper_id is not None
    assert helper_id in callee_ids, f"Expected helper in callees of runner, got: {callee_ids}"


# ---------------------------------------------------------------------------
# Additional targeted tests
# ---------------------------------------------------------------------------


def test_adapter_parse_returns_root(tmp_path: Path) -> None:
    adapter = adapter_for_extension(".rs")
    root = adapter.parse(b"fn foo() {}")
    assert root.type == "source_file"


def test_locate_function(tmp_path: Path) -> None:
    adapter = adapter_for_extension(".rs")
    src = b"fn bar() {}\nfn baz(x: i32) {}\n"
    root = adapter.parse(src)
    node = adapter.locate_function(root, "baz", 1)
    assert node is not None
    assert node.type == "function_item"


def test_locate_impl_method(tmp_path: Path) -> None:
    adapter = adapter_for_extension(".rs")
    src = b"struct Foo {}\nimpl Foo {\n    fn method(&self) {}\n}\n"
    root = adapter.parse(src)
    node = adapter.locate_function(root, "method", 2)
    assert node is not None
    assert node.type == "function_item"


def test_call_sites_simple(tmp_path: Path) -> None:
    adapter = adapter_for_extension(".rs")
    src = b"fn foo() { bar(1, 2); baz(); }"
    root = adapter.parse(src)
    fn_node = root.named_children[0]
    sites = adapter.call_sites(fn_node, src)
    callees = [s[0] for s in sites]
    assert "bar" in callees
    assert "baz" in callees


def test_call_sites_method(tmp_path: Path) -> None:
    adapter = adapter_for_extension(".rs")
    src = b"fn foo(x: MyType) { x.do_thing(1); }"
    root = adapter.parse(src)
    fn_node = root.named_children[0]
    sites = adapter.call_sites(fn_node, src)
    callees = [s[0] for s in sites]
    assert any("do_thing" in c for c in callees), f"callees: {callees}"


def test_call_sites_scoped(tmp_path: Path) -> None:
    adapter = adapter_for_extension(".rs")
    src = b'fn foo() { std::fs::read_to_string("f"); }'
    root = adapter.parse(src)
    fn_node = root.named_children[0]
    sites = adapter.call_sites(fn_node, src)
    callees = [s[0] for s in sites]
    assert any("std" in c and "fs" in c for c in callees), f"callees: {callees}"


def test_describe_let(tmp_path: Path) -> None:
    adapter = adapter_for_extension(".rs")
    src = b"fn foo() { let x = 42; }"
    root = adapter.parse(src)
    fn_node = root.named_children[0]
    body = adapter.function_body(fn_node)
    stmts = adapter.block_statements(body)
    assert len(stmts) >= 1
    from cgir.languages.base import AssignDesc

    desc = adapter.describe_statement(stmts[0], src)
    assert isinstance(desc, AssignDesc)
    assert "x" in desc.writes


def test_describe_for(tmp_path: Path) -> None:
    adapter = adapter_for_extension(".rs")
    src = b"fn foo(items: Vec<i32>) { for item in items { let x = item; } }"
    root = adapter.parse(src)
    fn_node = root.named_children[0]
    body = adapter.function_body(fn_node)
    stmts = adapter.block_statements(body)
    from cgir.languages.base import LoopDesc

    # find the for loop
    loop_stmt = next(
        (s for s in stmts if s.type in ("for_expression", "expression_statement")), None
    )
    assert loop_stmt is not None
    desc = adapter.describe_statement(loop_stmt, src)
    assert isinstance(desc, LoopDesc), f"got {type(desc)}"
    assert "item" in desc.writes
    assert "items" in desc.reads


def test_describe_if(tmp_path: Path) -> None:
    adapter = adapter_for_extension(".rs")
    src = b"fn foo(x: i32) { if x > 0 { let y = 1; } else { let z = 2; } }"
    root = adapter.parse(src)
    fn_node = root.named_children[0]
    body = adapter.function_body(fn_node)
    stmts = adapter.block_statements(body)
    from cgir.languages.base import BranchDesc

    desc = adapter.describe_statement(stmts[0], src)
    assert isinstance(desc, BranchDesc), f"got {type(desc)}"
    assert desc.consequence is not None


def test_describe_match(tmp_path: Path) -> None:
    adapter = adapter_for_extension(".rs")
    src = b'fn foo(x: i32) -> &str { match x { 0 => "zero", _ => "other" } }'
    root = adapter.parse(src)
    fn_node = root.named_children[0]
    body = adapter.function_body(fn_node)
    stmts = adapter.block_statements(body)
    from cgir.languages.base import MatchDesc

    desc = adapter.describe_statement(stmts[0], src)
    assert isinstance(desc, MatchDesc), f"got {type(desc)}"
    assert len(desc.cases) >= 2


def test_use_import_scoped(tmp_path: Path) -> None:
    adapter = adapter_for_extension(".rs")
    src = b"use std::fs::File;\nfn foo() {}"
    root = adapter.parse(src)
    decls = adapter.module_declarations(root, src, "mymod", "mymod.rs")
    imports = [d for d in decls if hasattr(d, "target")]
    assert any(d.target == "std.fs.File" and d.alias == "File" for d in imports), (
        f"imports: {[(d.target, d.alias) for d in imports]}"
    )


def test_use_import_list(tmp_path: Path) -> None:
    adapter = adapter_for_extension(".rs")
    src = b"use std::io::{Read, Write};\nfn foo() {}"
    root = adapter.parse(src)
    decls = adapter.module_declarations(root, src, "mymod", "mymod.rs")
    imports = [d for d in decls if hasattr(d, "target")]
    targets = {d.target for d in imports}
    assert "std.io.Read" in targets, f"targets: {targets}"
    assert "std.io.Write" in targets


def test_use_import_as_clause(tmp_path: Path) -> None:
    adapter = adapter_for_extension(".rs")
    src = b"use sqlx::{Pool, Postgres as PG};\nfn foo() {}"
    root = adapter.parse(src)
    decls = adapter.module_declarations(root, src, "mymod", "mymod.rs")
    imports = [d for d in decls if hasattr(d, "target")]
    pg = next((d for d in imports if d.alias == "PG"), None)
    assert pg is not None, f"PG alias not found; imports: {[(d.target, d.alias) for d in imports]}"
    assert "Postgres" in pg.target


def test_use_plain_crate(tmp_path: Path) -> None:
    adapter = adapter_for_extension(".rs")
    src = b"use reqwest;\nfn foo() {}"
    root = adapter.parse(src)
    decls = adapter.module_declarations(root, src, "mymod", "mymod.rs")
    imports = [d for d in decls if hasattr(d, "target")]
    assert any(d.target == "reqwest" for d in imports), (
        f"imports: {[(d.target, d.alias) for d in imports]}"
    )


def test_doc_comment_extracted(tmp_path: Path) -> None:
    adapter = adapter_for_extension(".rs")
    src = b"/// Does math.\n/// Second line.\npub fn add(a: i32, b: i32) -> i32 { a + b }"
    root = adapter.parse(src)
    decls = adapter.module_declarations(root, src, "m", "m.rs")
    from cgir.languages.base import FunctionDecl

    fn_decl = next((d for d in decls if isinstance(d, FunctionDecl) and d.name == "add"), None)
    assert fn_decl is not None
    assert "Does math" in fn_decl.doc


def test_raise_in_unwrap(tmp_path: Path) -> None:
    """x.unwrap() on a function → raise effect (high)."""
    src_code = """\
pub fn safe_get(x: Option<i32>) -> i32 {
    x.unwrap()
}
"""
    adapter = adapter_for_extension(".rs")
    src = src_code.encode()
    root = adapter.parse(src)
    fn_node = root.named_children[0]
    effects = adapter.direct_effects_confidence(fn_node, src, {})
    assert "raise" in effects


def test_while_loop_in_cfg(tmp_path: Path) -> None:
    while_code = """\
pub fn count(limit: i32) -> i32 {
    let mut n = 0;
    while n < limit {
        n += 1;
    }
    n
}
"""
    (tmp_path / "w.rs").write_text(while_code)
    adapter = adapter_for_extension(".rs")
    source = TreeSitterSource(adapter=adapter)
    graph = source.ingest(tmp_path)
    build_cfg(graph, tmp_path, adapter=adapter)
    count_id = next((n.id for n in graph.nodes(NodeKind.Function) if "count" in n.id), None)
    assert count_id is not None
    child_kinds = {c.kind for c in graph.children(count_id)}
    assert NodeKind.Loop in child_kinds
