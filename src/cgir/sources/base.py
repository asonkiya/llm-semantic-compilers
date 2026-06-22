"""Abstract GraphSource — every backend normalizes into the same RepoGraph."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from cgir.ir.graph import RepoGraph


class GraphSource(ABC):
    @abstractmethod
    def ingest(self, repo_path: Path) -> RepoGraph:
        """Walk ``repo_path`` and produce a normalized RepoGraph."""
