from pathlib import Path

from cgir.ir.nodes import NodeKind
from cgir.sources import TreeSitterSource


def test_ingests_fixture(python_sample_repo: Path) -> None:
    graph = TreeSitterSource().ingest(python_sample_repo)

    files = {n.name for n in graph.nodes(NodeKind.File)}
    assert files == {"pricing.py", "orchestrator.py"}

    funcs = {n.attrs.get("qualname") for n in graph.nodes(NodeKind.Function)}
    assert {"pricing.add_tax", "orchestrator.quote"} <= funcs

    params = {n.name for n in graph.nodes(NodeKind.Parameter)}
    assert {"price", "rate"} <= params
