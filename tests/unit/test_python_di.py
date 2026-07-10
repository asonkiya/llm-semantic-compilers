"""Python DI/field-type resolution — parity with the TypeScript adapter.

`self.<field>.<method>()` should resolve to the field's declared class the
same way `this.<field>.<method>()` does in TS. Field types come from three
Python idioms:

* ``__init__`` param annotation stored on self: ``def __init__(self, svc:
  Svc): self.svc = svc``
* class-level annotation (dataclass style): ``svc: Svc``
* direct construction: ``self.svc = Svc()``
"""

from __future__ import annotations

from pathlib import Path

from cgir.analyses.call_graph import build_call_graph
from cgir.analyses.effects import classify
from cgir.analyses.symbols import build_symbol_tables
from cgir.ir.edges import EdgeKind
from cgir.ir.nodes import NodeKind
from cgir.sources import TreeSitterSource


def _fields(tmp_path: Path, body: str) -> dict[str, str]:
    (tmp_path / "m.py").write_text(body)
    graph = TreeSitterSource().ingest(tmp_path)
    cls = next(n for n in graph.nodes(NodeKind.Class))
    return cls.attrs.get("fields") or {}


def test_init_param_annotation_stored_on_self(tmp_path: Path) -> None:
    fields = _fields(
        tmp_path,
        "class Reader:\n"
        "    def __init__(self, svc: ChaptersService, plain):\n"
        "        self.svc = svc\n"
        "        self.plain = plain\n",
    )
    assert fields.get("svc") == "ChaptersService"
    assert "plain" not in fields  # un-annotated param carries no type


def test_class_level_annotation(tmp_path: Path) -> None:
    fields = _fields(
        tmp_path,
        "class Reader:\n    svc: ChaptersService\n    count: int = 0\n",
    )
    assert fields.get("svc") == "ChaptersService"
    assert fields.get("count") == "int"


def test_direct_construction(tmp_path: Path) -> None:
    fields = _fields(
        tmp_path,
        "class Reader:\n    def __init__(self):\n        self.svc = ChaptersService()\n",
    )
    assert fields.get("svc") == "ChaptersService"


def _di_repo(tmp_path: Path) -> Path:
    (tmp_path / "chapters.py").write_text(
        "import requests\n\n"
        "class ChaptersService:\n"
        "    def translate(self, chapter_id):\n"
        "        return requests.post(f'/x/{chapter_id}')\n"
    )
    (tmp_path / "reader.py").write_text(
        "from chapters import ChaptersService\n\n"
        "class Reader:\n"
        "    def __init__(self, svc: ChaptersService):\n"
        "        self.svc = svc\n"
        "    def run(self):\n"
        "        return self.svc.translate(1)\n"
    )
    return tmp_path


def test_self_field_call_resolves_cross_file(tmp_path: Path) -> None:
    repo = _di_repo(tmp_path)
    graph = TreeSitterSource().ingest(repo)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, repo)
    callees = {e.dst for e in graph.out_edges("method:reader.Reader.run", EdgeKind.CALLS)}
    assert "method:chapters.ChaptersService.translate" in callees


def test_self_field_call_makes_caller_effectful(tmp_path: Path) -> None:
    repo = _di_repo(tmp_path)
    graph = TreeSitterSource().ingest(repo)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, repo)
    effects = classify(graph, repo)
    assert "calls_effectful" in effects["method:reader.Reader.run"]
