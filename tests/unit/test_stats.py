"""RED-phase tests for the codebase structure report.

Contract:

* ``compute_stats(specs) -> dict`` — pure function over ComponentSpecs,
  JSON-able result (drives both ``cgir stats`` text output and ``--json``).
* Keys: ``total``, ``files``, ``kinds``, ``purity``, ``effects``,
  ``most_called``, ``top_fan_out``, ``external_calls``.
* ``most_called`` ranks intra-repo callees by caller count;
  ``external_calls`` counts calls that don't resolve to a spec id.
"""

from __future__ import annotations

from cgir.ir.component_spec import ComponentKind, ComponentSpec
from cgir.report.stats import compute_stats


def _spec(
    spec_id: str,
    kind: ComponentKind = ComponentKind.pure_function,
    calls: list[str] | None = None,
    effects: list[str] | None = None,
    purity: float = 1.0,
    trace: list[str] | None = None,
) -> ComponentSpec:
    return ComponentSpec(
        id=spec_id,
        kind=kind,
        inputs=[],
        outputs=[],
        effects=effects or [],
        calls=calls or [],
        trace=trace or [f"{spec_id.split('.')[0]}.py:1"],
        language="python",
        signature=None,
        purity=purity,
    )


def _sample() -> list[ComponentSpec]:
    return [
        _spec("m.leaf"),
        _spec("m.mid", kind=ComponentKind.orchestrator, calls=["m.leaf"], purity=0.7),
        _spec(
            "n.top",
            kind=ComponentKind.effect_adapter,
            calls=["m.leaf", "m.mid", "json.dumps"],
            effects=["io"],
            purity=0.0,
        ),
    ]


def test_totals_and_files() -> None:
    stats = compute_stats(_sample())
    assert stats["total"] == 3
    assert stats["files"] == 2  # m.py and n.py


def test_kind_counts() -> None:
    stats = compute_stats(_sample())
    assert stats["kinds"] == {
        "pure_function": 1,
        "orchestrator": 1,
        "effect_adapter": 1,
    }


def test_purity_summary() -> None:
    stats = compute_stats(_sample())
    purity = stats["purity"]
    assert purity["pure"] == 1
    assert purity["tainted"] == 1
    assert purity["impure"] == 1
    assert abs(purity["mean"] - (1.0 + 0.7 + 0.0) / 3) < 1e-9


def test_effect_counts() -> None:
    stats = compute_stats(_sample())
    assert stats["effects"] == {"io": 1}


def test_most_called_ranks_by_caller_count() -> None:
    stats = compute_stats(_sample())
    assert stats["most_called"][0] == {"id": "m.leaf", "callers": 2}


def test_top_fan_out() -> None:
    stats = compute_stats(_sample())
    assert stats["top_fan_out"][0] == {"id": "n.top", "calls": 3}


def test_external_calls_counted() -> None:
    stats = compute_stats(_sample())
    assert {"id": "json.dumps", "callers": 1} in stats["external_calls"]


def test_top_constructed_counts_types() -> None:
    specs = _sample()
    specs[1].constructs = ["models.Chapter"]
    specs[2].constructs = ["models.Chapter", "models.Novel"]
    stats = compute_stats(specs)
    assert stats["top_constructed"][0] == {"id": "models.Chapter", "constructors": 2}


def test_entrypoints_listed() -> None:
    specs = _sample()
    specs[2].entrypoint = "HTTP GET /top"
    stats = compute_stats(specs)
    assert stats["entrypoints"] == [{"id": "n.top", "entrypoint": "HTTP GET /top"}]


def test_empty_specs() -> None:
    stats = compute_stats([])
    assert stats["total"] == 0
    assert stats["kinds"] == {}
    assert stats["purity"]["mean"] == 0.0
    assert stats["top_constructed"] == []
