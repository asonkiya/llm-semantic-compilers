"""`cgir impact --run` — execute exactly the tests the blast radius names."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from cgir.cli import app
from cgir.ir.component_spec import ComponentKind, ComponentSpec
from cgir.report.impact import runnable_selectors

runner = CliRunner()


def _spec(spec_id: str, trace: str, language: str = "python") -> ComponentSpec:
    return ComponentSpec(
        id=spec_id, kind=ComponentKind.pure_function, trace=[trace], language=language
    )


def test_selector_for_plain_test_function() -> None:
    specs = [_spec("tests.test_m.test_f", "tests/test_m.py:3")]
    selectors, skipped = runnable_selectors(specs, ["tests.test_m.test_f"])
    assert selectors == ["tests/test_m.py::test_f"]
    assert skipped == []


def test_selector_for_class_scoped_test() -> None:
    specs = [_spec("tests.test_m.TestX.test_f", "tests/test_m.py:9")]
    selectors, _ = runnable_selectors(specs, ["tests.test_m.TestX.test_f"])
    assert selectors == ["tests/test_m.py::TestX::test_f"]


def test_fixture_helpers_are_skipped_not_run() -> None:
    # covered_by can include fixture helpers in test files; they are not
    # pytest-collectable and must be skipped with a note, not passed to pytest.
    specs = [_spec("tests.test_m._make_thing", "tests/test_m.py:1")]
    selectors, skipped = runnable_selectors(specs, ["tests.test_m._make_thing"])
    assert selectors == []
    assert skipped == ["tests.test_m._make_thing"]


def test_non_python_tests_are_skipped() -> None:
    specs = [_spec("src.app.foo.spec.test_f", "src/app/foo.spec.ts:1", language="typescript")]
    selectors, skipped = runnable_selectors(specs, ["src.app.foo.spec.test_f"])
    assert selectors == []
    assert skipped == ["src.app.foo.spec.test_f"]


def test_impact_run_executes_selected_tests(tmp_path: Path) -> None:
    (tmp_path / "pricing.py").write_text(
        "def add_tax(price, rate):\n    return price * (1 + rate)\n"
    )
    (tmp_path / "test_pricing.py").write_text(
        "from pricing import add_tax\n\ndef test_add_tax():\n    assert add_tax(100, 0.5) == 150\n"
    )
    idx = tmp_path / ".cgir"
    assert runner.invoke(app, ["scan", str(tmp_path), "--out", str(idx)]).exit_code == 0
    result = runner.invoke(
        app, ["impact", "pricing.add_tax", "--index", str(idx), "--run", "--repo", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "1 passed" in result.output
