"""RED-phase tests for the index diff (Sprint 16 — effect-drift CI).

Contract:

* ``compute_diff(old_specs, new_specs) -> dict`` — pure over two spec
  lists, JSON-able. Keys: ``added``, ``removed``, ``changed``.
* ``changed`` entries report per-field old/new for the *contract* fields:
  kind, purity, effects, signature, outputs. Unchanged components are
  absent.
* ``violations(diff, rules) -> list[str]`` evaluates fail rules:
  ``effect-gain`` (any new tag on an existing component),
  ``effect-gain:<tag>`` (a specific tag), ``purity-drop``,
  ``kind-change``.
"""

from __future__ import annotations

from cgir.ir.component_spec import ComponentKind, ComponentSpec
from cgir.report.diff import compute_diff, violations


def _spec(
    spec_id: str,
    kind: ComponentKind = ComponentKind.pure_function,
    effects: list[str] | None = None,
    purity: float = 1.0,
    signature: str | None = None,
) -> ComponentSpec:
    return ComponentSpec(
        id=spec_id,
        kind=kind,
        inputs=[],
        outputs=[],
        effects=effects or [],
        calls=[],
        trace=[f"{spec_id.split('.')[0]}.py:1"],
        language="python",
        signature=signature,
        purity=purity,
    )


def test_added_and_removed() -> None:
    diff = compute_diff([_spec("m.old")], [_spec("m.new")])
    assert diff["added"] == ["m.new"]
    assert diff["removed"] == ["m.old"]


def test_unchanged_component_not_reported() -> None:
    diff = compute_diff([_spec("m.f")], [_spec("m.f")])
    assert diff["added"] == []
    assert diff["removed"] == []
    assert diff["changed"] == []


def test_effect_gain_reported() -> None:
    old = [_spec("m.f")]
    new = [_spec("m.f", kind=ComponentKind.effect_adapter, effects=["net"], purity=0.0)]
    diff = compute_diff(old, new)
    [change] = diff["changed"]
    assert change["id"] == "m.f"
    assert change["fields"]["effects"] == {"old": [], "new": ["net"]}
    assert change["fields"]["kind"] == {"old": "pure_function", "new": "effect_adapter"}
    assert change["fields"]["purity"] == {"old": 1.0, "new": 0.0}


def test_signature_change_reported() -> None:
    old = [_spec("m.f", signature="f(x)")]
    new = [_spec("m.f", signature="f(x, y)")]
    diff = compute_diff(old, new)
    [change] = diff["changed"]
    assert change["fields"]["signature"] == {"old": "f(x)", "new": "f(x, y)"}


def test_violation_effect_gain_any() -> None:
    diff = compute_diff([_spec("m.f")], [_spec("m.f", effects=["fs"], purity=0.0)])
    found = violations(diff, ["effect-gain"])
    assert len(found) == 1
    assert "m.f" in found[0] and "fs" in found[0]


def test_violation_effect_gain_specific_tag() -> None:
    diff = compute_diff([_spec("m.f")], [_spec("m.f", effects=["fs"], purity=0.0)])
    assert violations(diff, ["effect-gain:net"]) == []
    assert len(violations(diff, ["effect-gain:fs"])) == 1


def test_violation_purity_drop() -> None:
    diff = compute_diff([_spec("m.f", purity=1.0)], [_spec("m.f", purity=0.7)])
    assert len(violations(diff, ["purity-drop"])) == 1


def test_violation_kind_change() -> None:
    old = [_spec("m.f")]
    new = [_spec("m.f", kind=ComponentKind.orchestrator, purity=0.7)]
    assert len(violations(diff := compute_diff(old, new), ["kind-change"])) == 1
    assert violations(diff, []) == []


def test_no_violation_on_clean_diff() -> None:
    diff = compute_diff([_spec("m.f")], [_spec("m.f")])
    assert violations(diff, ["effect-gain", "purity-drop", "kind-change"]) == []


def test_new_components_do_not_trip_gain_rules() -> None:
    """Only *existing* components drifting counts; new effectful code is fine."""
    diff = compute_diff([], [_spec("m.f", effects=["net"], purity=0.0)])
    assert violations(diff, ["effect-gain", "purity-drop"]) == []
