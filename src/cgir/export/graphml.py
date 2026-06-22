"""GraphML export — milestone: P2-graphml."""

from __future__ import annotations

from pathlib import Path

from cgir.ir.graph import RepoGraph


def write(out_dir: Path, graph: RepoGraph) -> None:
    raise NotImplementedError("milestone: P2-graphml")
