"""Edge vocabulary for the CGIR graph (Code-IR.md §Data model)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EdgeKind(StrEnum):
    CONTAINS = "CONTAINS"
    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    READS = "READS"
    WRITES = "WRITES"
    MUTATES = "MUTATES"
    RETURNS = "RETURNS"
    THROWS = "THROWS"
    FLOWS_TO = "FLOWS_TO"
    CONTROLS = "CONTROLS"
    DEPENDS_ON = "DEPENDS_ON"
    TRACE_OF = "TRACE_OF"
    REGENERATED_AS = "REGENERATED_AS"


@dataclass(frozen=True, slots=True)
class Edge:
    src: str
    dst: str
    kind: EdgeKind
    attrs: dict[str, Any] = field(default_factory=dict)
