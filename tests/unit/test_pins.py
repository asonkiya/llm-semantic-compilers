"""Declarable contracts (pins) — `# cgir: pure`, `// cgir: no-net`, etc.

Pins let a developer declare what a component must *stay*, enforced by the
existing pipeline. Two classes:

* **state pins** (`pure`, `no-<tag>`) — checkable on a single scan; violations
  even in newly added code.
* **change pins** (`stable-signature`, `frozen`) — checkable on a scan pair;
  always evaluated (the pin is the opt-in, no --fail-on needed).
"""

from __future__ import annotations

from pathlib import Path

from cgir.analyses.effects import classify
from cgir.analyses.purity import score
from cgir.ir.component_spec import ComponentKind, ComponentSpec
from cgir.report.pins import change_violations, state_violations
from cgir.slicing import slice_components
from cgir.sources import TreeSitterSource


def _scan(tmp_path: Path) -> list[ComponentSpec]:
    graph = TreeSitterSource().ingest(tmp_path)
    effects = classify(graph, tmp_path)
    purity = score(graph, effects)
    return list(slice_components(graph, effects=effects, purity_scores=purity))


# --- extraction ---------------------------------------------------------------


def test_python_preceding_pin_extracted(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("# cgir: pure\ndef f(x):\n    return x\n")
    [spec] = _scan(tmp_path)
    assert spec.pins == ["pure"]


def test_python_trailing_pin_extracted(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("def f(x):  # cgir: no-net, no-fs\n    return x\n")
    [spec] = _scan(tmp_path)
    assert spec.pins == ["no-fs", "no-net"]


def test_python_pin_above_decorator(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("# cgir: frozen\n@staticmethod\ndef f(x):\n    return x\n")
    [spec] = _scan(tmp_path)
    assert spec.pins == ["frozen"]


def test_module_level_pin_applies_to_all(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "# cgir: no-net\n\ndef f(x):\n    return x\n\ndef g(x):\n    return x\n"
    )
    specs = _scan(tmp_path)
    assert all("no-net" in s.pins for s in specs)


def test_typescript_pin_extracted(tmp_path: Path) -> None:
    (tmp_path / "m.ts").write_text("// cgir: pure\nexport function f(x: number) { return x; }\n")
    [spec] = _scan(tmp_path)
    assert spec.pins == ["pure"]


def test_no_pin_means_empty(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("# just a comment\ndef f(x):\n    return x\n")
    [spec] = _scan(tmp_path)
    assert spec.pins == []


def test_method_pin_extracted(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "class C:\n    # cgir: stable-signature\n    def m(self, x):\n        return x\n"
    )
    spec = next(s for s in _scan(tmp_path) if s.id.endswith("C.m"))
    assert spec.pins == ["stable-signature"]


# --- state pins ----------------------------------------------------------------


def _spec(
    spec_id: str,
    pins: list[str] | None = None,
    effects: list[str] | None = None,
    calls: list[str] | None = None,
    kind: ComponentKind = ComponentKind.pure_function,
    signature: str | None = None,
    outputs: list[str] | None = None,
) -> ComponentSpec:
    return ComponentSpec(
        id=spec_id,
        kind=kind,
        effects=effects or [],
        calls=calls or [],
        pins=pins or [],
        signature=signature,
        outputs=outputs or [],
        trace=[f"{spec_id}.py:1"],
    )


def test_pure_pin_violated_by_direct_effect() -> None:
    specs = [_spec("m.f", pins=["pure"], effects=["net"], kind=ComponentKind.effect_adapter)]
    found = state_violations(specs)
    assert len(found) == 1
    assert "m.f" in found[0] and "pure" in found[0]


def test_pure_pin_violated_transitively() -> None:
    # f is pinned pure but calls g which does net.
    specs = [
        _spec(
            "m.f",
            pins=["pure"],
            effects=["calls_effectful"],
            calls=["m.g"],
            kind=ComponentKind.orchestrator,
        ),
        _spec("m.g", effects=["net"], kind=ComponentKind.effect_adapter),
    ]
    assert len(state_violations(specs)) == 1


def test_pure_pin_allows_raise() -> None:
    # raise is recorded but not impure (settled taxonomy).
    specs = [_spec("m.f", pins=["pure"], effects=["raise"])]
    assert state_violations(specs) == []


def test_no_tag_pin_checks_transitive_closure() -> None:
    # f pinned no-net; f -> g -> h(net) two hops away.
    specs = [
        _spec("m.f", pins=["no-net"], calls=["m.g"]),
        _spec("m.g", calls=["m.h"]),
        _spec("m.h", effects=["net"], kind=ComponentKind.effect_adapter),
    ]
    found = state_violations(specs)
    assert len(found) == 1 and "no-net" in found[0]


def test_no_tag_pin_satisfied() -> None:
    specs = [
        _spec("m.f", pins=["no-net"], effects=["fs"], kind=ComponentKind.effect_adapter),
    ]
    assert state_violations(specs) == []


def test_unknown_pin_flagged() -> None:
    specs = [_spec("m.f", pins=["no-such-pin"])]
    found = state_violations(specs)
    assert len(found) == 1 and "unknown pin" in found[0]


# --- change pins ----------------------------------------------------------------


def test_stable_signature_pin_blocks_signature_change() -> None:
    old = [_spec("m.f", pins=["stable-signature"], signature="f(x)")]
    new = [_spec("m.f", pins=["stable-signature"], signature="f(x, y)")]
    found = change_violations(old, new)
    assert len(found) == 1 and "stable-signature" in found[0]


def test_stable_signature_pin_allows_body_change() -> None:
    old = [_spec("m.f", pins=["stable-signature"], signature="f(x)")]
    new = [
        _spec(
            "m.f",
            pins=["stable-signature"],
            signature="f(x)",
            effects=["io"],
            kind=ComponentKind.effect_adapter,
        )
    ]
    assert change_violations(old, new) == []


def test_frozen_pin_blocks_any_contract_change() -> None:
    old = [_spec("m.f", pins=["frozen"])]
    new = [_spec("m.f", pins=["frozen"], effects=["io"], kind=ComponentKind.effect_adapter)]
    assert len(change_violations(old, new)) == 1


def test_frozen_pin_blocks_removal() -> None:
    old = [_spec("m.f", pins=["frozen"])]
    found = change_violations(old, [])
    assert len(found) == 1 and "removed" in found[0]


def test_pin_read_from_new_side() -> None:
    # Adding a pin in the same change activates it.
    old = [_spec("m.f", signature="f(x)")]
    new = [_spec("m.f", pins=["stable-signature"], signature="f(x, y)")]
    assert len(change_violations(old, new)) == 1


# --- surfaces -------------------------------------------------------------------


def test_pack_renders_pins() -> None:
    from cgir.report.pack import build_pack, render_pack

    specs = [_spec("m.f", pins=["pure", "no-net"])]
    out = render_pack(build_pack(specs, "m.f"))
    assert "Pinned: no-net, pure" in out or "Pinned: pure, no-net" in out
