"""Map ``path:line`` source locations to ComponentSpec ids.

Used by ``cgir trace`` to answer "which component owns this line?". For now
we index by start/end-line ranges per Function/Method. The future PDG pass
will refine this to statement-level traces.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import NodeKind


@dataclass(frozen=True, slots=True)
class _Span:
    path: str
    start: int
    end: int
    component_id: str


class TraceMap:
    def __init__(self, spans: list[_Span]) -> None:
        self._by_path: dict[str, list[_Span]] = {}
        for span in spans:
            self._by_path.setdefault(span.path, []).append(span)

    def lookup(self, path: str, line: int) -> str | None:
        for span in self._by_path.get(path, []):
            if span.start <= line <= span.end:
                return span.component_id
        return None

    def to_jsonable(self) -> list[dict[str, str | int]]:
        return [
            {"path": s.path, "start": s.start, "end": s.end, "component_id": s.component_id}
            for spans in self._by_path.values()
            for s in spans
        ]

    @classmethod
    def from_jsonable(cls, data: list[dict[str, str | int]]) -> TraceMap:
        spans = [
            _Span(
                path=str(d["path"]),
                start=int(d["start"]),
                end=int(d["end"]),
                component_id=str(d["component_id"]),
            )
            for d in data
        ]
        return cls(spans)

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_jsonable(), indent=2, sort_keys=True))

    @classmethod
    def read(cls, path: Path) -> TraceMap:
        return cls.from_jsonable(json.loads(path.read_text()))


def build_trace_map(graph: RepoGraph) -> TraceMap:
    spans: list[_Span] = []
    for node in graph.nodes():
        if node.kind not in {NodeKind.Function, NodeKind.Method}:
            continue
        if node.path is None or node.start_line is None or node.end_line is None:
            continue
        qual = node.attrs.get("qualname") if node.attrs else None
        component_id = str(qual) if isinstance(qual, str) else node.name
        spans.append(
            _Span(
                path=node.path,
                start=node.start_line,
                end=node.end_line,
                component_id=component_id,
            )
        )
    return TraceMap(spans)
