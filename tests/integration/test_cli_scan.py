import json
from pathlib import Path
from textwrap import dedent

from typer.testing import CliRunner

from cgir.cli import app


def _write(repo: Path, rel: str, body: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dedent(body).lstrip())


def test_scan_writes_outputs(tmp_path: Path, python_sample_repo: Path) -> None:
    runner = CliRunner()
    out_dir = tmp_path / "cgir-out"
    result = runner.invoke(app, ["scan", str(python_sample_repo), "--out", str(out_dir)])
    assert result.exit_code == 0, result.output

    repo_graph = json.loads((out_dir / "repo_graph.json").read_text())
    kinds = {n["kind"] for n in repo_graph["nodes"]}
    assert {"Repository", "File", "Module", "Function"} <= kinds

    add_tax = json.loads((out_dir / "components" / "pricing.add_tax.json").read_text())
    assert add_tax["id"] == "pricing.add_tax"
    assert add_tax["inputs"] == ["price", "rate"]

    trace_map = json.loads((out_dir / "trace_map.json").read_text())
    assert any(s["component_id"] == "pricing.add_tax" for s in trace_map)


def test_scan_prints_per_kind_histogram(tmp_path: Path, python_sample_repo: Path) -> None:
    runner = CliRunner()
    out_dir = tmp_path / "cgir-out"
    result = runner.invoke(app, ["scan", str(python_sample_repo), "--out", str(out_dir)])
    assert result.exit_code == 0, result.output
    # Both fixture functions are pure_function (no effects).
    assert "pure_function" in result.output
    # The histogram should report the count per kind.
    assert "pure_function: 2" in result.output


def test_scan_reports_total_components(tmp_path: Path, python_sample_repo: Path) -> None:
    runner = CliRunner()
    out_dir = tmp_path / "cgir-out"
    result = runner.invoke(app, ["scan", str(python_sample_repo), "--out", str(out_dir)])
    assert "2 components" in result.output


def test_scan_exclude_flag(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo, "keep.py", "def keep():\n    pass\n")
    _write(repo, "vendor/lib.py", "def skip():\n    pass\n")

    runner = CliRunner()
    out_dir = tmp_path / "out"
    result = runner.invoke(app, ["scan", str(repo), "--out", str(out_dir), "--exclude", "vendor"])
    assert result.exit_code == 0, result.output

    index = json.loads((out_dir / "components_index.json").read_text())
    ids = {entry["id"] for entry in index}
    assert "keep.keep" in ids
    assert not any("skip" in i for i in ids)


# --- viz + graphml export (Sprint 6) ------------------------------------------


def _scanned_index(tmp_path: Path, python_sample_repo: Path) -> Path:
    out_dir = tmp_path / "cgir-out"
    result = CliRunner().invoke(app, ["scan", str(python_sample_repo), "--out", str(out_dir)])
    assert result.exit_code == 0, result.output
    return out_dir


def test_viz_writes_html(tmp_path: Path, python_sample_repo: Path) -> None:
    out_dir = _scanned_index(tmp_path, python_sample_repo)
    result = CliRunner().invoke(app, ["viz", "--index", str(out_dir)])
    assert result.exit_code == 0, result.output
    html = (out_dir / "viz.html").read_text()
    assert "pricing.add_tax" in html


def test_viz_mermaid_prints_flowchart(tmp_path: Path, python_sample_repo: Path) -> None:
    out_dir = _scanned_index(tmp_path, python_sample_repo)
    result = CliRunner().invoke(app, ["viz", "--index", str(out_dir), "--format", "mermaid"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("flowchart")
    assert "orchestrator_quote --> pricing_add_tax" in result.output


def test_export_graphml(tmp_path: Path, python_sample_repo: Path) -> None:
    out_dir = _scanned_index(tmp_path, python_sample_repo)
    result = CliRunner().invoke(app, ["export", "--format", "graphml", "--out", str(out_dir)])
    assert result.exit_code == 0, result.output
    assert (out_dir / "repo_graph.graphml").exists()


# --- stats (Sprint 7) ----------------------------------------------------------


def test_stats_prints_summary(tmp_path: Path, python_sample_repo: Path) -> None:
    out_dir = _scanned_index(tmp_path, python_sample_repo)
    result = CliRunner().invoke(app, ["stats", "--index", str(out_dir)])
    assert result.exit_code == 0, result.output
    assert "Components: 2" in result.output
    assert "pure_function" in result.output
    assert "pricing.add_tax" in result.output  # most-called on the fixture


def test_stats_json_output(tmp_path: Path, python_sample_repo: Path) -> None:
    out_dir = _scanned_index(tmp_path, python_sample_repo)
    result = CliRunner().invoke(app, ["stats", "--index", str(out_dir), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total"] == 2
    assert payload["kinds"]["pure_function"] == 2
