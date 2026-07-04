"""RepoGraph: a thin wrapper over networkx.MultiDiGraph with CGIR semantics."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import networkx as nx

from cgir.ir.edges import Edge, EdgeKind
from cgir.ir.nodes import Node, NodeKind


class RepoGraph:
    def __init__(self) -> None:
        self._g: nx.MultiDiGraph = nx.MultiDiGraph()

    def add_node(self, node: Node) -> None:
        self._g.add_node(
            node.id,
            kind=node.kind,
            name=node.name,
            path=node.path,
            start_line=node.start_line,
            end_line=node.end_line,
            attrs=dict(node.attrs),
        )

    def add_edge(self, edge: Edge) -> None:
        self._g.add_edge(
            edge.src, edge.dst, key=edge.kind.value, kind=edge.kind, attrs=dict(edge.attrs)
        )

    def has_node(self, node_id: str) -> bool:
        return node_id in self._g

    def get_node(self, node_id: str) -> Node:
        data = self._g.nodes[node_id]
        return Node(
            id=node_id,
            kind=data["kind"],
            name=data["name"],
            path=data.get("path"),
            start_line=data.get("start_line"),
            end_line=data.get("end_line"),
            attrs=dict(data.get("attrs") or {}),
        )

    def nodes(self, kind: NodeKind | None = None) -> Iterator[Node]:
        for node_id, data in self._g.nodes(data=True):
            if kind is None or data["kind"] == kind:
                yield Node(
                    id=node_id,
                    kind=data["kind"],
                    name=data["name"],
                    path=data.get("path"),
                    start_line=data.get("start_line"),
                    end_line=data.get("end_line"),
                    attrs=dict(data.get("attrs") or {}),
                )

    def out_edges(self, node_id: str, kind: EdgeKind | None = None) -> Iterator[Edge]:
        for src, dst, data in self._g.out_edges(node_id, data=True):
            if kind is None or data["kind"] == kind:
                yield Edge(src=src, dst=dst, kind=data["kind"], attrs=dict(data.get("attrs") or {}))

    def in_edges(self, node_id: str, kind: EdgeKind | None = None) -> Iterator[Edge]:
        for src, dst, data in self._g.in_edges(node_id, data=True):
            if kind is None or data["kind"] == kind:
                yield Edge(src=src, dst=dst, kind=data["kind"], attrs=dict(data.get("attrs") or {}))

    def children(self, node_id: str, kind: NodeKind | None = None) -> Iterator[Node]:
        for edge in self.out_edges(node_id, EdgeKind.CONTAINS):
            child = self.get_node(edge.dst)
            if kind is None or child.kind == kind:
                yield child

    def __len__(self) -> int:
        return int(self._g.number_of_nodes())

    @classmethod
    def from_jsonable(cls, data: dict[str, Any]) -> RepoGraph:
        """Inverse of :meth:`to_jsonable` — rebuild a graph from its JSON dump."""
        graph = cls()
        for n in data.get("nodes", []):
            graph.add_node(
                Node(
                    id=n["id"],
                    kind=NodeKind(n["kind"]),
                    name=n["name"],
                    path=n.get("path"),
                    start_line=n.get("start_line"),
                    end_line=n.get("end_line"),
                    attrs=dict(n.get("attrs") or {}),
                )
            )
        for e in data.get("edges", []):
            graph.add_edge(
                Edge(
                    src=e["src"],
                    dst=e["dst"],
                    kind=EdgeKind(e["kind"]),
                    attrs=dict(e.get("attrs") or {}),
                )
            )
        return graph

    def to_jsonable(self) -> dict[str, Any]:
        nodes = []
        for node in self.nodes():
            nodes.append(
                {
                    "id": node.id,
                    "kind": node.kind.value,
                    "name": node.name,
                    "path": node.path,
                    "start_line": node.start_line,
                    "end_line": node.end_line,
                    "attrs": node.attrs,
                }
            )
        edges = []
        for src, dst, data in self._g.edges(data=True):
            edges.append(
                {
                    "src": src,
                    "dst": dst,
                    "kind": data["kind"].value,
                    "attrs": data.get("attrs") or {},
                }
            )
        return {"nodes": nodes, "edges": edges}
