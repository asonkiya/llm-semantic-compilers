"""Node vocabulary for the CGIR graph (Code-IR.md §Data model)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class NodeKind(StrEnum):
    Repository = "Repository"
    File = "File"
    Module = "Module"
    Class = "Class"
    Function = "Function"
    Method = "Method"
    Parameter = "Parameter"
    Variable = "Variable"
    Assignment = "Assignment"
    Expr = "Expr"
    Statement = "Statement"
    Branch = "Branch"
    Loop = "Loop"
    Return = "Return"
    Import = "Import"
    Effect = "Effect"
    Test = "Test"


@dataclass(frozen=True, slots=True)
class Node:
    id: str
    kind: NodeKind
    name: str
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
