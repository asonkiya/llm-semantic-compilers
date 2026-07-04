"""RED-phase tests for the Mermaid call-graph renderer.

Contract:

* ``render_call_graph(specs) -> str`` consumes ``list[ComponentSpec]`` and
  returns Mermaid ``flowchart`` text.
* One node per component, grouped into a ``subgraph`` per source file
  (from ``trace``); one edge per resolved intra-repo call.
* Node ids are sanitized (Mermaid ids can't contain dots); labels keep the
  dotted component id.
* Components are styled by kind via ``classDef`` + ``class`` lines.
* Calls to components outside the spec set (stdlib, third-party) are
  skipped rather than rendered as dangling nodes.
"""

from __future__ import annotations

from cgir.export.mermaid import render_call_graph
from cgir.ir.component_spec import ComponentKind, ComponentSpec


def _spec(
    spec_id: str,
    kind: ComponentKind = ComponentKind.pure_function,
    calls: list[str] | None = None,
    trace: list[str] | None = None,
) -> ComponentSpec:
    return ComponentSpec(
        id=spec_id,
        kind=kind,
        inputs=[],
        outputs=[],
        effects=[],
        calls=calls or [],
        trace=trace or [f"{spec_id.split('.')[0]}.py:1"],
        language="python",
        signature=None,
        purity=1.0,
    )


def test_renders_flowchart_header() -> None:
    text = render_call_graph([_spec("m.f")])
    assert text.startswith("flowchart")


def test_component_appears_with_dotted_label() -> None:
    text = render_call_graph([_spec("pricing.add_tax")])
    assert "pricing.add_tax" in text


def test_call_edge_rendered() -> None:
    specs = [
        _spec("pricing.add_tax"),
        _spec("orchestrator.quote", calls=["pricing.add_tax"]),
    ]
    text = render_call_graph(specs)
    # Sanitized ids joined by an arrow.
    assert "orchestrator_quote --> pricing_add_tax" in text


def test_subgraph_per_source_file() -> None:
    specs = [
        _spec("pricing.add_tax", trace=["pricing.py:1"]),
        _spec("orchestrator.quote", trace=["orchestrator.py:3"]),
    ]
    text = render_call_graph(specs)
    assert text.count("subgraph") == 2
    assert "pricing.py" in text
    assert "orchestrator.py" in text


def test_kind_styling_present() -> None:
    specs = [_spec("m.f", kind=ComponentKind.effect_adapter)]
    text = render_call_graph(specs)
    assert "classDef effect_adapter" in text
    assert "class m_f effect_adapter" in text


def test_external_calls_are_skipped() -> None:
    specs = [_spec("m.f", calls=["json.dumps"])]
    text = render_call_graph(specs)
    assert "json_dumps" not in text
