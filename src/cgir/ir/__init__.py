"""Internal IR: nodes, edges, RepoGraph, and ComponentSpec."""

from cgir.ir.component_spec import ComponentKind, ComponentSpec
from cgir.ir.edges import Edge, EdgeKind
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import Node, NodeKind

__all__ = [
    "ComponentKind",
    "ComponentSpec",
    "Edge",
    "EdgeKind",
    "Node",
    "NodeKind",
    "RepoGraph",
]
