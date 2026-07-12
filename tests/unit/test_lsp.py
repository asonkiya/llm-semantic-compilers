"""LSP diagnostics — live contract drift as editor squiggles.

The pure core (`compute_diagnostics`, `DiagnosticsEngine`) is tested here;
pygls wiring is a thin layer behind the `cgir[lsp]` extra (lazy import,
same pattern as mcp/llm).
"""

from __future__ import annotations

from pathlib import Path

from cgir.api.lsp_server import DiagnosticsEngine, compute_diagnostics
from cgir.ir.component_spec import ComponentKind, ComponentSpec


def _spec(
    spec_id: str,
    kind: ComponentKind = ComponentKind.pure_function,
    effects: list[str] | None = None,
    pins: list[str] | None = None,
    trace: str | None = None,
) -> ComponentSpec:
    return ComponentSpec(
        id=spec_id,
        kind=kind,
        effects=effects or [],
        pins=pins or [],
        trace=[trace or f"{spec_id.split('.')[0]}.py:1"],
    )


def test_pin_violation_is_error_at_component_line() -> None:
    new = [
        _spec(
            "m.f", kind=ComponentKind.effect_adapter, effects=["io"], pins=["pure"], trace="m.py:7"
        )
    ]
    diags = compute_diagnostics([], new)
    assert len(diags) == 1
    d = diags[0]
    assert d.path == "m.py" and d.line == 7 and d.severity == "error"
    assert "pinned pure" in d.message


def test_gate_drift_is_warning() -> None:
    old = [_spec("m.f", trace="m.py:3")]
    new = [_spec("m.f", kind=ComponentKind.effect_adapter, effects=["net"], trace="m.py:3")]
    diags = compute_diagnostics(old, new)
    assert any(d.severity == "warning" and "net" in d.message for d in diags)


def test_clean_scan_no_diagnostics() -> None:
    specs = [_spec("m.f")]
    assert compute_diagnostics(specs, specs) == []


def test_unlocatable_violation_dropped() -> None:
    # a violation naming a component with no trace can't be placed — skip it
    new = [
        ComponentSpec(
            id="m.g", kind=ComponentKind.effect_adapter, effects=["io"], pins=["pure"], trace=[]
        )
    ]
    assert compute_diagnostics([], new) == []


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "m.py").write_text("# cgir: pure\ndef score(x):\n    return x * 2\n")
    return tmp_path


def test_engine_first_refresh_reports_absolute_pin_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    engine = DiagnosticsEngine(repo, tmp_path / ".cgir")
    diags = engine.refresh()
    assert diags == []  # pinned pure and actually pure


def test_engine_detects_violation_then_clears_on_revert(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    engine = DiagnosticsEngine(repo, tmp_path / ".cgir")
    engine.refresh()

    (repo / "m.py").write_text("# cgir: pure\ndef score(x):\n    print(x)\n    return x * 2\n")
    diags = engine.refresh()
    assert any(d.severity == "error" and "pinned pure" in d.message for d in diags)

    (repo / "m.py").write_text("# cgir: pure\ndef score(x):\n    return x * 2\n")
    assert engine.refresh() == []


def test_engine_drift_warning_between_refreshes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "n.py").write_text("def fetch(url):\n    return url\n")
    engine = DiagnosticsEngine(repo, tmp_path / ".cgir")
    engine.refresh()
    (repo / "n.py").write_text("import requests\n\ndef fetch(url):\n    return requests.get(url)\n")
    diags = engine.refresh()
    assert any(d.severity == "warning" and "net" in d.message and d.path == "n.py" for d in diags)


def test_create_server_without_pygls_raises_install_hint() -> None:
    import importlib.util

    import pytest

    from cgir.api.lsp_server import create_server

    if importlib.util.find_spec("pygls") is not None:
        pytest.skip("pygls installed; missing-dependency path not reachable")
    with pytest.raises(RuntimeError, match="cgir\\[lsp\\]"):
        create_server()
