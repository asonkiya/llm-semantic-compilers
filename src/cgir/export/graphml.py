"""GraphML export — opens the RepoGraph in Gephi, yEd, or Cytoscape.

GraphML attribute values must be scalars, so list/dict attrs are
JSON-encoded strings and ``None`` attrs are dropped. ``kind`` carries the
Node/Edge enum value.
"""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx

from cgir.ir.graph import RepoGraph


def write(out_dir: Path, graph: RepoGraph) -> Path:
    """Write ``<out_dir>/repo_graph.graphml`` and return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    flat: nx.MultiDiGraph = nx.MultiDiGraph()
    for node in graph.nodes():
        attrs: dict[str, str | int | float | bool] = {
            "kind": node.kind.value,
            "name": node.name,
        }
        if node.path is not None:
            attrs["path"] = node.path
        if node.start_line is not None:
            attrs["start_line"] = node.start_line
        if node.end_line is not None:
            attrs["end_line"] = node.end_line
        for key, value in node.attrs.items():
            scalar = _to_scalar(value)
            if scalar is not None:
                attrs[key] = scalar
        flat.add_node(node.id, **attrs)
    for node in graph.nodes():
        for edge in graph.out_edges(node.id):
            flat.add_edge(edge.src, edge.dst, kind=edge.kind.value)

    path = out_dir / "repo_graph.graphml"
    nx.write_graphml(flat, path)
    return path


def _to_scalar(value: object) -> str | int | float | bool | None:
    if value is None:
        return None
    if isinstance(value, str | int | float | bool):
        return value
    return json.dumps(value)
