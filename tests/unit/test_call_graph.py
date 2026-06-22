from pathlib import Path

from cgir.analyses.call_graph import build_call_graph
from cgir.analyses.symbols import build_symbol_tables
from cgir.ir.edges import EdgeKind
from cgir.sources import TreeSitterSource


def test_orchestrator_calls_pricing(python_sample_repo: Path) -> None:
    graph = TreeSitterSource().ingest(python_sample_repo)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, python_sample_repo)

    quote_id = "func:orchestrator.quote"
    add_tax_id = "func:pricing.add_tax"
    callees = {e.dst for e in graph.out_edges(quote_id, EdgeKind.CALLS)}
    assert add_tax_id in callees
