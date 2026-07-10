"""RED-phase tests for change-impact / blast-radius analysis.

Contract:

* ``compute_impact(specs, target_id) -> dict`` — pure over ComponentSpecs.
  Answers "if I change ``target_id``, what is affected?" via the transitive
  *upstream* (caller) closure, plus the derived surface at risk:
  - ``direct_callers``  — immediate callers
  - ``affected``        — transitive caller closure (excludes target)
  - ``entrypoints``     — components in {target} + affected that are
                          externally reachable (blast radius reaching the API)
  - ``tests``           — union of ``covered_by`` over target + affected
                          (the deterministic set of tests to run)
* ``render_impact(specs, target_id) -> str`` — human summary.
"""

from __future__ import annotations

from cgir.ir.component_spec import ComponentKind, ComponentSpec
from cgir.report.impact import compute_impact, compute_typed_impact, render_impact


def _spec(
    spec_id: str,
    calls: list[str] | None = None,
    covered_by: list[str] | None = None,
    entrypoint: str | None = None,
) -> ComponentSpec:
    return ComponentSpec(
        id=spec_id,
        kind=ComponentKind.pure_function,
        calls=calls or [],
        covered_by=covered_by or [],
        entrypoint=entrypoint,
        trace=[f"{spec_id}.py:1"],
    )


def test_direct_and_transitive_callers() -> None:
    # a -> b -> c  (a calls b, b calls c)
    specs = [_spec("m.a", calls=["m.b"]), _spec("m.b", calls=["m.c"]), _spec("m.c")]
    imp = compute_impact(specs, "m.c")
    assert imp["direct_callers"] == ["m.b"]
    assert imp["affected"] == ["m.a", "m.b"]


def test_target_excluded_from_affected() -> None:
    specs = [_spec("m.a", calls=["m.b"]), _spec("m.b")]
    assert "m.b" not in compute_impact(specs, "m.b")["affected"]


def test_entrypoints_in_blast_radius() -> None:
    specs = [
        _spec("routes.handler", calls=["svc.do"], entrypoint="HTTP GET /x"),
        _spec("svc.do", calls=["util.helper"]),
        _spec("util.helper"),
    ]
    imp = compute_impact(specs, "util.helper")
    assert imp["entrypoints"] == [{"id": "routes.handler", "entrypoint": "HTTP GET /x"}]


def test_target_itself_counts_as_entrypoint() -> None:
    specs = [_spec("routes.h", entrypoint="HTTP POST /y")]
    imp = compute_impact(specs, "routes.h")
    assert imp["entrypoints"] == [{"id": "routes.h", "entrypoint": "HTTP POST /y"}]


def test_tests_to_run_union() -> None:
    specs = [
        _spec("m.a", calls=["m.b"], covered_by=["t.a"]),
        _spec("m.b", calls=["m.c"], covered_by=["t.b"]),
        _spec("m.c", covered_by=["t.c"]),
    ]
    # changing c -> run c's tests and every affected caller's tests
    assert compute_impact(specs, "m.c")["tests"] == ["t.a", "t.b", "t.c"]


def test_leaf_with_no_callers() -> None:
    specs = [_spec("m.solo", covered_by=["t.solo"])]
    imp = compute_impact(specs, "m.solo")
    assert imp["direct_callers"] == []
    assert imp["affected"] == []
    assert imp["entrypoints"] == []
    assert imp["tests"] == ["t.solo"]


def test_cycle_terminates() -> None:
    # a <-> b mutual recursion; must not loop forever
    specs = [_spec("m.a", calls=["m.b"]), _spec("m.b", calls=["m.a"])]
    imp = compute_impact(specs, "m.a")
    assert imp["affected"] == ["m.b"]


def test_unknown_target_raises() -> None:
    try:
        compute_impact([_spec("m.a")], "m.missing")
    except KeyError:
        return
    raise AssertionError("expected KeyError for unknown target")


def test_test_callers_partition_into_tests_not_affected() -> None:
    # A test function that calls the target is a *test to run*, not affected
    # production code — even when it reaches the target transitively and was
    # never linked via covered_by.
    specs = [
        _spec("m.helper"),
        _spec("m.api", calls=["m.helper"]),
        _spec("tests.test_api.test_roundtrip", calls=["m.api"]),
    ]
    imp = compute_impact(specs, "m.helper")
    assert imp["affected"] == ["m.api"]
    assert "tests.test_api.test_roundtrip" in imp["tests"]


def test_render_separates_tests_from_affected() -> None:
    specs = [
        _spec("m.f"),
        _spec("tests.test_m.test_f", calls=["m.f"]),
    ]
    out = render_impact(specs, "m.f")
    assert "0 component(s) affected" in out
    assert "tests.test_m.test_f" in out


# --- typed impact: blast radius narrowed by *what* changed --------------------


def _chain() -> list[ComponentSpec]:
    # a -> b -> c, a is an entrypoint, each has its own test
    return [
        _spec("m.a", calls=["m.b"], covered_by=["t.a"], entrypoint="HTTP GET /a"),
        _spec("m.b", calls=["m.c"], covered_by=["t.b"]),
        _spec("m.c", covered_by=["t.c"]),
    ]


def test_body_only_change_has_no_downstream_impact() -> None:
    # nothing in the contract changed — callers are contract-safe.
    imp = compute_typed_impact(_chain(), "m.c", [])
    assert imp["reach"] == "none"
    assert imp["affected"] == []
    assert imp["entrypoints"] == []
    assert imp["tests"] == ["t.c"]  # only the target's own tests


def test_effect_change_propagates_transitively() -> None:
    # effects/purity taint flows up the whole call chain.
    imp = compute_typed_impact(_chain(), "m.c", ["effects"])
    assert imp["reach"] == "transitive"
    assert imp["affected"] == ["m.a", "m.b"]
    assert imp["entrypoints"] == [{"id": "m.a", "entrypoint": "HTTP GET /a"}]
    assert imp["tests"] == ["t.a", "t.b", "t.c"]


def test_signature_change_stops_at_direct_callers() -> None:
    # an interface break forces the *direct* call sites to adapt, but does
    # not inherently ripple past them.
    imp = compute_typed_impact(_chain(), "m.c", ["signature"])
    assert imp["reach"] == "direct"
    assert imp["affected"] == ["m.b"]  # not m.a
    assert imp["tests"] == ["t.b", "t.c"]


def test_widest_reach_wins_when_multiple_fields_change() -> None:
    imp = compute_typed_impact(_chain(), "m.c", ["signature", "effects"])
    assert imp["reach"] == "transitive"
    assert imp["affected"] == ["m.a", "m.b"]


def test_purity_and_kind_also_propagate_transitively() -> None:
    assert compute_typed_impact(_chain(), "m.c", ["purity"])["reach"] == "transitive"
    assert compute_typed_impact(_chain(), "m.c", ["kind"])["reach"] == "transitive"


def test_typed_reports_changed_fields_and_direct_callers() -> None:
    imp = compute_typed_impact(_chain(), "m.c", ["signature"])
    assert imp["changed_fields"] == ["signature"]
    assert imp["direct_callers"] == ["m.b"]  # graph reality, informational


def test_render_impact_is_human_summary() -> None:
    specs = [
        _spec("routes.h", calls=["svc.do"], entrypoint="HTTP GET /x"),
        _spec("svc.do", calls=["util.helper"], covered_by=["t.svc"]),
        _spec("util.helper"),
    ]
    out = render_impact(specs, "util.helper")
    assert "util.helper" in out
    assert "routes.h" in out  # affected caller shown
    assert "HTTP GET /x" in out  # entrypoint at risk
    assert "t.svc" in out  # test to run
