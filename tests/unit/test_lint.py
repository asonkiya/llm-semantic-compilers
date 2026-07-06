"""RED-phase tests for cgir lint (Sprint 26 — semantic architecture rules).

Contract:

* ``lint(specs, rules) -> list[LintViolation]`` — pure over specs. Rules
  are dicts scoped by an ``in`` id-glob and carrying one predicate:
    - ``forbid-effect``: matched components must not carry these effect tags
    - ``require-kind``: matched components must be this kind
    - ``forbid-call``: matched components must not call components whose id
      matches this glob (a semantic layer boundary over resolved CALLS)
* These express constraints an *import* linter cannot: effects, component
  kind, and call targets — not just module imports.
"""

from __future__ import annotations

from cgir.ir.component_spec import ComponentKind, ComponentSpec
from cgir.report.lint import lint


def _spec(
    spec_id: str,
    kind: ComponentKind = ComponentKind.pure_function,
    effects: list[str] | None = None,
    calls: list[str] | None = None,
) -> ComponentSpec:
    return ComponentSpec(
        id=spec_id,
        kind=kind,
        effects=effects or [],
        calls=calls or [],
        trace=[f"{spec_id.replace('.', '/')}.py:1"],
    )


def test_forbid_effect_flags_violation() -> None:
    specs = [
        _spec("aspen.zones.contains", effects=["net"]),
        _spec("aspen.zones.suggest", effects=[]),
    ]
    rules = [{"name": "pure-core", "in": "aspen.zones.*", "forbid-effect": ["net", "fs"]}]
    viol = lint(specs, rules)
    assert len(viol) == 1
    assert viol[0].component == "aspen.zones.contains"
    assert "net" in viol[0].detail


def test_forbid_effect_clean_passes() -> None:
    specs = [_spec("aspen.zones.contains", effects=["raise"])]
    rules = [{"name": "pure-core", "in": "aspen.zones.*", "forbid-effect": ["net"]}]
    assert lint(specs, rules) == []


def test_require_kind_flags_violation() -> None:
    specs = [_spec("aspen.zones.x", kind=ComponentKind.effect_adapter, effects=["io"])]
    rules = [{"name": "zones-pure", "in": "aspen.zones.*", "require-kind": "pure_function"}]
    viol = lint(specs, rules)
    assert len(viol) == 1
    assert "effect_adapter" in viol[0].detail


def test_forbid_call_flags_layer_break() -> None:
    specs = [
        _spec("aspen.actions.run", calls=["aspen.api.db.save"]),
        _spec("aspen.api.db.save", kind=ComponentKind.effect_adapter),
    ]
    rules = [{"name": "no-actions-to-api", "in": "aspen.actions.*", "forbid-call": "aspen.api.*"}]
    viol = lint(specs, rules)
    assert len(viol) == 1
    assert "aspen.api.db.save" in viol[0].detail


def test_forbid_call_allowed_target_passes() -> None:
    specs = [_spec("aspen.actions.run", calls=["aspen.zones.contains"])]
    rules = [{"name": "no-actions-to-api", "in": "aspen.actions.*", "forbid-call": "aspen.api.*"}]
    assert lint(specs, rules) == []


def test_scope_glob_limits_rule() -> None:
    """A rule only applies to components matching its `in` glob."""
    specs = [_spec("other.mod", effects=["net"])]
    rules = [{"name": "pure-core", "in": "aspen.zones.*", "forbid-effect": ["net"]}]
    assert lint(specs, rules) == []
