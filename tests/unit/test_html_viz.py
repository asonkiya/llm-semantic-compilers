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
