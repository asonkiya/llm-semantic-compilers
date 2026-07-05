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


# --- flow tracing (Sprint 14) ---------------------------------------------------


def test_flow_shows_downstream_callees(tmp_path: Path, python_sample_repo: Path) -> None:
    out_dir = _scanned_index(tmp_path, python_sample_repo)
    result = CliRunner().invoke(app, ["flow", "orchestrator.quote", "--index", str(out_dir)])
    assert result.exit_code == 0, result.output
    assert "pricing.add_tax" in result.output
    assert "calls" in result.output.lower()


def test_flow_shows_upstream_callers(tmp_path: Path, python_sample_repo: Path) -> None:
    out_dir = _scanned_index(tmp_path, python_sample_repo)
    result = CliRunner().invoke(app, ["flow", "pricing.add_tax", "--index", str(out_dir)])
    assert result.exit_code == 0, result.output
    assert "orchestrator.quote" in result.output
    assert "called by" in result.output.lower()


def test_flow_unknown_component_fails(tmp_path: Path, python_sample_repo: Path) -> None:
    out_dir = _scanned_index(tmp_path, python_sample_repo)
    result = CliRunner().invoke(app, ["flow", "nope.nothing", "--index", str(out_dir)])
    assert result.exit_code != 0


# --- pack (Sprint 18 — context packer) ------------------------------------------


def test_pack_emits_bundle_with_source(tmp_path: Path, python_sample_repo: Path) -> None:
    out_dir = _scanned_index(tmp_path, python_sample_repo)
    result = CliRunner().invoke(
        app,
        ["pack", "orchestrator.quote", "--index", str(out_dir), "--repo", str(python_sample_repo)],
    )
    assert result.exit_code == 0, result.output
    assert result.output.startswith("# orchestrator.quote")
    # Callee interface present, and the target's real source embedded.
    assert "pricing.add_tax" in result.output
    assert "def quote" in result.output


def test_pack_unknown_component_fails(tmp_path: Path, python_sample_repo: Path) -> None:
    out_dir = _scanned_index(tmp_path, python_sample_repo)
    result = CliRunner().invoke(app, ["pack", "nope.x", "--index", str(out_dir)])
    assert result.exit_code != 0


# --- diff (Sprint 16 — effect-drift CI) ----------------------------------------


def _drifted_repos(tmp_path: Path) -> tuple[Path, Path]:
    """Two scans of the same function, where the new one gained io."""
    old_repo = tmp_path / "old-repo"
    new_repo = tmp_path / "new-repo"
    _write(old_repo, "pricing.py", "def add_tax(price, rate):\n    return price * (1 + rate)\n")
    _write(
        new_repo,
        "pricing.py",
        "def add_tax(price, rate):\n    print(price)\n    return price * (1 + rate)\n",
    )
    runner = CliRunner()
    old_idx = tmp_path / "old-idx"
    new_idx = tmp_path / "new-idx"
    assert runner.invoke(app, ["scan", str(old_repo), "--out", str(old_idx)]).exit_code == 0
    assert runner.invoke(app, ["scan", str(new_repo), "--out", str(new_idx)]).exit_code == 0
    return old_idx, new_idx


def test_diff_reports_drift(tmp_path: Path) -> None:
    old_idx, new_idx = _drifted_repos(tmp_path)
    result = CliRunner().invoke(app, ["diff", str(old_idx), str(new_idx)])
    assert result.exit_code == 0, result.output
    assert "pricing.add_tax" in result.output
    assert "io" in result.output


def test_diff_fail_on_effect_gain(tmp_path: Path) -> None:
    old_idx, new_idx = _drifted_repos(tmp_path)
    result = CliRunner().invoke(
        app, ["diff", str(old_idx), str(new_idx), "--fail-on", "effect-gain"]
    )
    assert result.exit_code == 1
    assert "pricing.add_tax" in result.output


def test_diff_identical_indexes_clean(tmp_path: Path, python_sample_repo: Path) -> None:
    idx = _scanned_index(tmp_path, python_sample_repo)
    result = CliRunner().invoke(app, ["diff", str(idx), str(idx), "--fail-on", "effect-gain"])
    assert result.exit_code == 0, result.output
    assert "no changes" in result.output.lower()


def test_diff_json_output(tmp_path: Path) -> None:
    old_idx, new_idx = _drifted_repos(tmp_path)
    result = CliRunner().invoke(app, ["diff", str(old_idx), str(new_idx), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["changed"][0]["id"] == "pricing.add_tax"
