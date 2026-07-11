"""Data-shape contracts — "the rewrite dropped a field" becomes drift.

Shape lives on the *type* (Class-node ``fields``, built for DI): TypedDict/
dataclass/pydantic bodies in Python, ``interface`` / object type-aliases in
TS. ``compute_diff`` gains a ``types`` section; the ``shape-change`` rule
fires when a drifted type is referenced by a component's contract.
"""

from __future__ import annotations

from pathlib import Path

from cgir.ir.component_spec import ComponentKind, ComponentSpec
from cgir.ir.nodes import NodeKind
from cgir.report.diff import compute_diff, violations
from cgir.sources import TreeSitterSource


def _type_fields(tmp_path: Path, name: str, body: str, ext: str = "py") -> dict[str, str]:
    (tmp_path / f"m.{ext}").write_text(body)
    graph = TreeSitterSource().ingest(tmp_path)
    cls = next(n for n in graph.nodes(NodeKind.Class) if n.name == name)
    return cls.attrs.get("fields") or {}


# --- extraction ---------------------------------------------------------------


def test_python_typeddict_fields(tmp_path: Path) -> None:
    fields = _type_fields(
        tmp_path,
        "Snapshot",
        "from typing import TypedDict\n\nclass Snapshot(TypedDict):\n    day: str\n    total: int\n",
    )
    assert set(fields) == {"day", "total"}


def test_ts_interface_fields(tmp_path: Path) -> None:
    fields = _type_fields(
        tmp_path,
        "Novel",
        "export interface Novel {\n  id: number;\n  title: string;\n  author?: string;\n}\n",
        ext="ts",
    )
    assert set(fields) == {"id", "title", "author"}
    assert fields["title"] == "string"


def test_ts_object_type_alias_fields(tmp_path: Path) -> None:
    fields = _type_fields(
        tmp_path,
        "Point",
        "export type Point = {\n  x: number;\n  y: number;\n};\n",
        ext="ts",
    )
    assert set(fields) == {"x", "y"}


# --- diff types section ---------------------------------------------------------


def _spec(spec_id: str, outputs: list[str] | None = None) -> ComponentSpec:
    return ComponentSpec(
        id=spec_id,
        kind=ComponentKind.pure_function,
        outputs=outputs or [],
        trace=[f"{spec_id}.py:1"],
    )


def test_diff_reports_dropped_field() -> None:
    old_types = {"m.Snapshot": {"day": "str", "total": "int"}}
    new_types = {"m.Snapshot": {"day": "str"}}
    specs = [_spec("m.rollup", outputs=["Snapshot"])]
    diff = compute_diff(specs, specs, old_types=old_types, new_types=new_types)
    [changed] = diff["types"]["changed"]
    assert changed["name"] == "m.Snapshot"
    assert changed["removed"] == ["total"]
    assert changed["referenced_by"] == ["m.rollup"]


def test_diff_reports_field_type_change() -> None:
    old_types = {"m.T": {"x": "int"}}
    new_types = {"m.T": {"x": "str"}}
    diff = compute_diff([], [], old_types=old_types, new_types=new_types)
    [changed] = diff["types"]["changed"]
    assert changed["changed"] == {"x": {"old": "int", "new": "str"}}


def test_diff_unchanged_types_not_reported() -> None:
    types = {"m.T": {"x": "int"}}
    diff = compute_diff([], [], old_types=types, new_types=types)
    assert diff["types"]["changed"] == []


def test_diff_without_types_keeps_shape() -> None:
    diff = compute_diff([_spec("m.f")], [_spec("m.f")])
    assert diff["types"] == {"changed": []}


# --- shape-change rule -----------------------------------------------------------


def test_shape_change_fires_when_referenced() -> None:
    old_types = {"m.Snapshot": {"day": "str", "total": "int"}}
    new_types = {"m.Snapshot": {"day": "str"}}
    specs = [_spec("m.rollup", outputs=["Snapshot"])]
    diff = compute_diff(specs, specs, old_types=old_types, new_types=new_types)
    found = violations(diff, ["shape-change"])
    assert len(found) == 1
    assert "m.Snapshot" in found[0] and "total" in found[0]


def test_shape_change_silent_when_unreferenced() -> None:
    old_types = {"m.Internal": {"x": "int"}}
    new_types = {"m.Internal": {}}
    diff = compute_diff([], [], old_types=old_types, new_types=new_types)
    assert violations(diff, ["shape-change"]) == []


def test_shape_change_not_evaluated_without_rule() -> None:
    old_types = {"m.Snapshot": {"total": "int"}}
    new_types = {"m.Snapshot": {}}
    specs = [_spec("m.rollup", outputs=["Snapshot"])]
    diff = compute_diff(specs, specs, old_types=old_types, new_types=new_types)
    assert violations(diff, ["effect-gain"]) == []
