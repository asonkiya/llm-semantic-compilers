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
from cgir.report.impact import compute_impact, render_impact


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
