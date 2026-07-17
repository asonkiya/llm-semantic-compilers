"""`cgir decompose` — rung 2: suggest functional-core / imperative-shell splits.

For an impure function, find the pure computational core (statements with no
effects and no dependence on effect results) and *suggest* the extraction —
advisory only, no code rewriting. The safety net for acting on a suggestion
is the extract → pin `pure` → verify loop.
"""

from __future__ import annotations

from pathlib import Path

from cgir.analyses.call_graph import build_call_graph
from cgir.analyses.cfg import build as build_cfg
from cgir.analyses.decompose import decompose, decompose_all
from cgir.analyses.effects import classify
from cgir.analyses.pdg import build as build_pdg
from cgir.analyses.symbols import build_symbol_tables
from cgir.sources import TreeSitterSource


def _graph(tmp_path: Path):
    graph = TreeSitterSource().ingest(tmp_path)
    tables = build_symbol_tables(graph)
    build_call_graph(graph, tables, tmp_path)
    build_cfg(graph, tmp_path)
    build_pdg(graph)
    effects = classify(graph, tmp_path)
    return graph, effects


COMPUTE_THEN_ACT = """\
import requests


def report(items, rate):
    total = 0
    for it in items:
        total = total + it
    taxed = total * (1 + rate)
    label = "big" if taxed > 100 else "small"
    requests.post("http://x", json={"t": taxed, "l": label})
    return taxed
"""


def test_compute_then_act_is_decomposable(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(COMPUTE_THEN_ACT)
    graph, effects = _graph(tmp_path)
    result = decompose(graph, effects, "func:m.report", tmp_path)
    assert result.decomposable
    # the post + anything after it is shell; the arithmetic is core
    assert result.core_statements >= 3
    assert "items" in result.inputs and "rate" in result.inputs
    assert "taxed" in result.outputs or "label" in result.outputs
    assert any("net" in tag for tag in result.shell_effects)


def test_already_pure_function_not_flagged(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("def add(a, b):\n    return a + b\n")
    graph, effects = _graph(tmp_path)
    result = decompose(graph, effects, "func:m.add", tmp_path)
    assert not result.decomposable
    assert result.reason == "already pure"


def test_fully_effectful_not_decomposable(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("import requests\n\n\ndef ping(url):\n    requests.get(url)\n")
    graph, effects = _graph(tmp_path)
    result = decompose(graph, effects, "func:m.ping", tmp_path)
    assert not result.decomposable


def test_effect_downstream_data_is_shell(tmp_path: Path) -> None:
    # act-then-compute: the fetch result taints downstream computation...
    (tmp_path / "m.py").write_text(
        "import requests\n\n\n"
        "def fetch_len(url, pad):\n"
        "    resp = requests.get(url)\n"
        "    n = len(resp.text)\n"  # depends on effect result -> shell
        "    padded = pad * 2\n"  # independent of the effect -> core candidate
        "    extra = padded + 1\n"
        "    final = extra * 3\n"
        "    return n + final\n"
    )
    graph, effects = _graph(tmp_path)
    result = decompose(graph, effects, "func:m.fetch_len", tmp_path, min_core=3)
    assert result.decomposable
    assert "pad" in result.inputs
    # the effect-tainted names must not be core outputs
    assert "resp" not in result.outputs and "n" not in result.outputs


def test_min_core_threshold(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "import requests\n\n\ndef f(x):\n    y = x + 1\n    requests.post('u', json=y)\n"
    )
    graph, effects = _graph(tmp_path)
    result = decompose(graph, effects, "func:m.f", tmp_path, min_core=3)
    assert not result.decomposable
    assert "core too small" in result.reason


def test_impure_callee_counts_as_shell(tmp_path: Path) -> None:
    # a call to an in-repo effectful helper is effectful even with no direct tags
    (tmp_path / "m.py").write_text(
        "import requests\n\n\n"
        "def send(x):\n    requests.post('u', json=x)\n\n\n"
        "def wrap(items):\n"
        "    total = 0\n"
        "    for it in items:\n"
        "        total = total + it\n"
        "    doubled = total * 2\n"
        "    send(doubled)\n"
        "    return doubled\n"
    )
    graph, effects = _graph(tmp_path)
    result = decompose(graph, effects, "func:m.wrap", tmp_path, min_core=3)
    assert result.decomposable
    assert any("calls send" in s or "send" in s for s in result.shell_effects)


def test_decompose_all_metric(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(COMPUTE_THEN_ACT + "\n\ndef pure(a):\n    return a * 2\n")
    graph, effects = _graph(tmp_path)
    report = decompose_all(graph, effects, tmp_path)
    assert report["impure_functions"] >= 1
    assert report["decomposable"] >= 1
    assert 0 <= report["decomposability_pct"] <= 100
