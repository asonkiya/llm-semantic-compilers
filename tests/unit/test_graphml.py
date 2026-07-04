"""RED-phase tests for GraphML export (milestone: P2-graphml).

Contract:

* ``write(out_dir, graph) -> Path`` writes ``<out_dir>/repo_graph.graphml``
  and returns its path.
* The file is valid GraphML: ``networkx.read_graphml`` can load it and
  finds the same node ids.
* GraphML values must be scalars — ``kind`` is the enum value string,
  list/dict attrs are JSON-encoded, ``None`` attrs are dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx

from cgir.export import graphml
from cgir.ir import Edge, EdgeKind, Node, NodeKind, RepoGraph


def _sample_graph() -> RepoGraph:
    g = RepoGraph()
    g.add_node(Node(id="m", kind=NodeKind.Module, name="m", path="m.py"))
    g.add_node(
        Node(
            id="func:m.f",
            kind=NodeKind.Function,
            name="f",
            path="m.py",
            start_line=1,
            end_line=3,
            attrs={"qualname": "m.f", "writes": ["x"], "controlled_by": None},
        )
    )
    g.add_edge(Edge(src="m", dst="func:m.f", kind=EdgeKind.CONTAINS))
    return g


def test_write_creates_graphml_file(tmp_path: Path) -> None:
    out = graphml.write(tmp_path, _sample_graph())
    assert out == tmp_path / "repo_graph.graphml"
    assert out.exists()


def test_graphml_loads_back_with_networkx(tmp_path: Path) -> None:
    out = graphml.write(tmp_path, _sample_graph())
    loaded = nx.read_graphml(out)
    assert set(loaded.nodes) == {"m", "func:m.f"}
    assert loaded.number_of_edges() == 1


def test_graphml_node_attrs_are_scalars(tmp_path: Path) -> None:
    out = graphml.write(tmp_path, _sample_graph())
    loaded = nx.read_graphml(out)
    func = loaded.nodes["func:m.f"]
    assert func["kind"] == "Function"
    assert func["start_line"] == 1
    # List attrs survive as JSON strings.
    assert json.loads(func["writes"]) == ["x"]
    # None-valued attrs are dropped, not stringified.
    assert "controlled_by" not in func


def test_graphml_edge_kind_preserved(tmp_path: Path) -> None:
    out = graphml.write(tmp_path, _sample_graph())
    loaded = nx.read_graphml(out)
    kinds = {data["kind"] for _, _, data in loaded.edges(data=True)}
    assert kinds == {"CONTAINS"}
