import json
from pathlib import Path

from typer.testing import CliRunner

from cgir.cli import app


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
