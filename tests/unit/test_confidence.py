"""Confidence tiers on effects — provenance for every tag.

`high`: exact/prefix table match (import-alias verified where applicable).
`lexical`: bare-suffix or receiver-name heuristics (`self.now()`, db-receiver
gating) — the measured false-positive class from gate-noise.md.

Surfaces: `spec.lexical_effects` (subset of `effects`); gate rules fire on
high-confidence tags by default, `:any` opts into lexical; renders mark
lexical tags with a trailing `?`.
"""

from __future__ import annotations

from pathlib import Path

from cgir.analyses.call_graph import build_call_graph
from cgir.analyses.effects import classify_with_confidence
from cgir.analyses.symbols import build_symbol_tables
from cgir.ir.component_spec import ComponentKind, ComponentSpec
from cgir.report.diff import compute_diff, violations
from cgir.sources import TreeSitterSource


def _effects(tmp_path: Path, code: str):
    (tmp_path / "m.py").write_text(code)
    graph = TreeSitterSource().ingest(tmp_path)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, tmp_path)
    return classify_with_confidence(graph, tmp_path)


def test_exact_match_is_high_confidence(tmp_path: Path) -> None:
    effects, lexical = _effects(
        tmp_path, "import requests\n\ndef f():\n    return requests.get('x')\n"
    )
    assert "net" in effects["func:m.f"]
    assert "net" not in lexical.get("func:m.f", [])


def test_suffix_match_is_lexical(tmp_path: Path) -> None:
    # `.now()` on an unknown receiver — the measured false-positive class.
    effects, lexical = _effects(tmp_path, "def f(clock):\n    return clock.now()\n")
    assert "nondeterm" in effects["func:m.f"]
    assert "nondeterm" in lexical.get("func:m.f", [])


def test_db_receiver_gating_is_lexical(tmp_path: Path) -> None:
    effects, lexical = _effects(tmp_path, "def f(db):\n    return db.query('x')\n")
    assert "db" in effects["func:m.f"]
    assert "db" in lexical.get("func:m.f", [])


def test_high_wins_when_both_match(tmp_path: Path) -> None:
    # datetime.now is in the exact nondeterm table AND matches the .now suffix.
    effects, lexical = _effects(tmp_path, "import time\n\ndef f():\n    return time.time()\n")
    assert "nondeterm" in effects["func:m.f"]
    assert "nondeterm" not in lexical.get("func:m.f", [])


def test_transitive_confidence_follows_callee(tmp_path: Path) -> None:
    # caller of a lexical-only-impure callee gets lexical calls_effectful
    effects, lexical = _effects(
        tmp_path,
        "def leaf(clock):\n    return clock.now()\n\n\ndef top(clock):\n    return leaf(clock)\n",
    )
    assert "calls_effectful" in effects["func:m.top"]
    assert "calls_effectful" in lexical.get("func:m.top", [])


def test_spec_carries_lexical_effects(tmp_path: Path) -> None:
    from cgir.analyses.purity import score
    from cgir.slicing import slice_components

    (tmp_path / "m.py").write_text("def f(clock):\n    return clock.now()\n")
    graph = TreeSitterSource().ingest(tmp_path)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, tmp_path)
    effects, lexical = classify_with_confidence(graph, tmp_path)
    purity = score(graph, effects)
    [spec] = slice_components(graph, effects=effects, purity_scores=purity, lexical_effects=lexical)
    assert spec.effects == ["nondeterm"]
    assert spec.lexical_effects == ["nondeterm"]


# --- gate behavior ---------------------------------------------------------------


def _spec(spec_id: str, effects=None, lexical=None) -> ComponentSpec:
    return ComponentSpec(
        id=spec_id,
        kind=ComponentKind.pure_function,
        effects=effects or [],
        lexical_effects=lexical or [],
        trace=[f"{spec_id}.py:1"],
    )


def test_lexical_gain_suppressed_by_default() -> None:
    diff = compute_diff(
        [_spec("m.f")], [_spec("m.f", effects=["nondeterm"], lexical=["nondeterm"])]
    )
    assert violations(diff, ["effect-gain:nondeterm"]) == []
    assert violations(diff, ["effect-gain"]) == []


def test_any_suffix_opts_into_lexical() -> None:
    diff = compute_diff(
        [_spec("m.f")], [_spec("m.f", effects=["nondeterm"], lexical=["nondeterm"])]
    )
    assert len(violations(diff, ["effect-gain:nondeterm:any"])) == 1


def test_high_confidence_gain_still_fires() -> None:
    diff = compute_diff([_spec("m.f")], [_spec("m.f", effects=["net"])])
    assert len(violations(diff, ["effect-gain:net"])) == 1


def test_lexical_loss_suppressed_by_default() -> None:
    # losing a tag that was only ever lexical evidence isn't a regression
    diff = compute_diff([_spec("m.f", effects=["db"], lexical=["db"])], [_spec("m.f")])
    assert violations(diff, ["effect-loss:db"]) == []
    assert len(violations(diff, ["effect-loss:db:any"])) == 1
