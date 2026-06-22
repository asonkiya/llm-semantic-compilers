"""Joern adapter — milestone: P2-joern-bridge."""

from __future__ import annotations

from pathlib import Path

from cgir.ir.graph import RepoGraph
from cgir.sources.base import GraphSource


class JoernSource(GraphSource):
    def ingest(self, repo_path: Path) -> RepoGraph:
        raise NotImplementedError("milestone: P2-joern-bridge")
