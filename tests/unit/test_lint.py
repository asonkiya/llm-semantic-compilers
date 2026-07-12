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


# --- forbid-cycle (market: import-linter/ArchUnit parity, over CALLS) ------------


def test_cycle_detected() -> None:
    specs = [
        _spec("m.a", calls=["m.b"]),
        _spec("m.b", calls=["m.c"]),
        _spec("m.c", calls=["m.a"]),
    ]
    found = lint(specs, [{"name": "no cycles", "forbid-cycle": True}])
    assert len(found) == 1
    assert "m.a" in found[0].detail and "m.c" in found[0].detail


def test_self_recursion_is_not_a_cycle() -> None:
    specs = [_spec("m.fact", calls=["m.fact"])]
    assert lint(specs, [{"name": "no cycles", "forbid-cycle": True}]) == []


def test_cycle_scope_respected() -> None:
    specs = [
        _spec("app.a", calls=["app.b"]),
        _spec("app.b", calls=["app.a"]),
        _spec("other.x", calls=["other.y"]),
        _spec("other.y", calls=["other.x"]),
    ]
    found = lint(specs, [{"name": "core acyclic", "in": "app.*", "forbid-cycle": True}])
    assert len(found) == 1
    assert "app.a" in found[0].detail


def test_no_cycle_clean() -> None:
    specs = [_spec("m.a", calls=["m.b"]), _spec("m.b")]
    assert lint(specs, [{"name": "no cycles", "forbid-cycle": True}]) == []


# --- layers (dependencies point down; same-layer fine) ---------------------------


LAYER_RULE = {"name": "layered", "layers": ["app.api.*", "app.core.*", "app.db.*"]}


def test_lower_layer_calling_higher_fires() -> None:
    specs = [
        _spec("app.api.route", calls=["app.core.logic"]),
        _spec("app.core.logic", calls=["app.db.save"]),
        _spec("app.db.save", calls=["app.api.route"]),  # db -> api: violation
    ]
    found = lint(specs, [LAYER_RULE])
    assert len(found) == 1
    assert "app.db.save" in found[0].component
    assert "app.api.route" in found[0].detail


def test_downward_and_same_layer_calls_pass() -> None:
    specs = [
        _spec("app.api.route", calls=["app.db.save", "app.api.helper"]),  # skip layers ok
        _spec("app.api.helper"),
        _spec("app.db.save"),
    ]
    assert lint(specs, [LAYER_RULE]) == []


def test_unmatched_components_ignored_by_layers() -> None:
    specs = [
        _spec("app.db.save", calls=["vendor.util.log"]),
        _spec("vendor.util.log", calls=["app.api.route"]),  # not in any layer
        _spec("app.api.route"),
    ]
    assert lint(specs, [LAYER_RULE]) == []
