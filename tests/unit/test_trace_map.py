from pathlib import Path

from cgir.sources import TreeSitterSource
from cgir.trace import build_trace_map


def test_lookup(python_sample_repo: Path) -> None:
    graph = TreeSitterSource().ingest(python_sample_repo)
    tm = build_trace_map(graph)
    assert tm.lookup("pricing.py", 1) == "pricing.add_tax"
    assert tm.lookup("pricing.py", 999) is None
