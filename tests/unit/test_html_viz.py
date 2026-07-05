"""RED-phase tests for the self-contained HTML visualization.

Contract:

* ``write(out_dir, specs) -> Path`` writes ``<out_dir>/viz.html`` and
  returns its path.
* The page is fully self-contained: no ``http://`` / ``https://`` loads
  (local-first — the viz must work offline, matching the no-network rule
  for the analysis layers).
* Component data (ids, kinds, purity, effects, calls) is embedded as JSON.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from cgir.export.html_viz import write
from cgir.ir.component_spec import ComponentKind, ComponentSpec


def _spec(
    spec_id: str,
    kind: ComponentKind = ComponentKind.pure_function,
    calls: list[str] | None = None,
    effects: list[str] | None = None,
) -> ComponentSpec:
    return ComponentSpec(
        id=spec_id,
        kind=kind,
        inputs=[],
        outputs=[],
        effects=effects or [],
        calls=calls or [],
        trace=[f"{spec_id.split('.')[0]}.py:1"],
        language="python",
        signature=None,
        purity=1.0,
    )


def test_write_creates_viz_html(tmp_path: Path) -> None:
    out = write(tmp_path, [_spec("m.f")])
    assert out == tmp_path / "viz.html"
    assert out.exists()
    assert out.read_text().lstrip().startswith("<!DOCTYPE html>")


def test_component_data_embedded(tmp_path: Path) -> None:
    specs = [
        _spec("pricing.add_tax"),
        _spec("orchestrator.quote", kind=ComponentKind.orchestrator, calls=["pricing.add_tax"]),
    ]
    html = write(tmp_path, specs).read_text()
    assert "pricing.add_tax" in html
    assert "orchestrator.quote" in html
    assert "orchestrator" in html  # kind value


def test_no_external_resources(tmp_path: Path) -> None:
    html = write(tmp_path, [_spec("m.f")]).read_text()
    assert "http://" not in html
    assert "https://" not in html


def test_embedded_json_is_parseable(tmp_path: Path) -> None:
    """The data island between the CGIR_DATA markers must be valid JSON."""
    html = write(tmp_path, [_spec("m.f", effects=["io"])]).read_text()
    match = re.search(r"/\*CGIR_DATA\*/(.*?)/\*END_CGIR_DATA\*/", html, re.DOTALL)
    assert match, "expected a /*CGIR_DATA*/ ... /*END_CGIR_DATA*/ island"
    data = json.loads(match.group(1))
    assert data["nodes"][0]["id"] == "m.f"
    assert data["nodes"][0]["effects"] == ["io"]


def _data_island(tmp_path: Path, specs: list[ComponentSpec]) -> dict:
    html = write(tmp_path, specs).read_text()
    match = re.search(r"/\*CGIR_DATA\*/(.*?)/\*END_CGIR_DATA\*/", html, re.DOTALL)
    assert match
    return json.loads(match.group(1))


def test_call_edges_carry_kind_and_return_type(tmp_path: Path) -> None:
    """Sprint 14: edges are typed — kind 'call' + the callee's return type."""
    callee = _spec("m.leaf")
    callee.outputs = ["float"]
    caller = _spec("m.top", calls=["m.leaf"])
    data = _data_island(tmp_path, [callee, caller])
    [edge] = data["edges"]
    assert edge["kind"] == "call"
    assert edge["type"] == "float"


def test_param_types_parsed_from_signature(tmp_path: Path) -> None:
    """Flow view needs input types; parsed bracket-aware from the signature."""
    spec = _spec("m.f")
    spec.signature = "f(price: float, table: dict[str, int], x=1) -> float"
    data = _data_island(tmp_path, [spec])
    assert data["nodes"][0]["param_types"] == ["float", "dict[str, int]", "?"]


def test_param_types_empty_without_signature(tmp_path: Path) -> None:
    data = _data_island(tmp_path, [_spec("m.f")])
    assert data["nodes"][0]["param_types"] == []


def test_arg_flow_edges_embedded(tmp_path: Path) -> None:
    """Sprint 15: PDG-derived arg edges (caller -> callee, typed by param)."""
    callee = _spec("m.save")
    caller = _spec("m.create", calls=["m.save"])
    caller.signature = "create(db: Session, payload: dict) -> None"
    arg_flows = {"m.create": [{"callee": "m.save", "params": ["db"]}]}
    html = write(tmp_path, [callee, caller], arg_flows=arg_flows).read_text()
    match = re.search(r"/\*CGIR_DATA\*/(.*?)/\*END_CGIR_DATA\*/", html, re.DOTALL)
    assert match
    data = json.loads(match.group(1))
    arg_edges = [e for e in data["edges"] if e["kind"] == "arg"]
    assert len(arg_edges) == 1
    assert arg_edges[0]["type"] == "Session"
    # direction: caller -> callee
    ids = [n["id"] for n in data["nodes"]]
    assert ids[arg_edges[0]["s"]] == "m.create"
    assert ids[arg_edges[0]["t"]] == "m.save"


def test_construct_edges_target_type_nodes(tmp_path: Path) -> None:
    """Constructs become dashed edges to synthetic type nodes."""
    spec = _spec("m.make")
    spec.constructs = ["models.Chapter"]
    data = _data_island(tmp_path, [spec])
    type_nodes = [n for n in data["nodes"] if n.get("kind") == "type"]
    assert [n["id"] for n in type_nodes] == ["models.Chapter"]
    [edge] = data["edges"]
    assert edge["kind"] == "construct"
