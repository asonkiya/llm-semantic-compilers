from pathlib import Path

from cgir.analyses.call_graph import build_call_graph
from cgir.analyses.effects import classify
from cgir.analyses.purity import score
from cgir.analyses.symbols import build_symbol_tables
from cgir.ir.component_spec import ComponentKind
from cgir.slicing import slice_components
from cgir.sources import TreeSitterSource


def test_pricing_add_tax_spec(python_sample_repo: Path) -> None:
    graph = TreeSitterSource().ingest(python_sample_repo)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, python_sample_repo)
    effects = classify(graph, python_sample_repo)
    purity = score(graph, effects)

    specs = {
        spec.id: spec for spec in slice_components(graph, effects=effects, purity_scores=purity)
    }
    add_tax = specs["pricing.add_tax"]

    assert add_tax.inputs == ["price", "rate"]
    assert add_tax.calls == []
    assert add_tax.trace == ["pricing.py:1"]
    assert add_tax.language == "python"
    assert add_tax.kind == ComponentKind.pure_function
    assert add_tax.purity == 1.0
    add_tax.validate()

    quote = specs["orchestrator.quote"]
    assert "pricing.add_tax" in quote.calls
    assert quote.kind == ComponentKind.pure_function
