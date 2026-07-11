"""`cgir init` — one-command onboarding."""

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from cgir.cli import app

runner = CliRunner()


def _repo(tmp_path: Path, git: bool = False) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def f(x):\n    return x\n")
    if git:
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    return repo


def test_init_scans_and_writes_config(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = runner.invoke(app, ["init", str(repo)])
    assert result.exit_code == 0, result.output
    assert (repo / ".cgir" / "components_index.json").exists()
    assert (repo / "cgir.toml").exists()
    assert "pure_function" in result.output  # the "what your repo is" summary


def test_init_gitignores_index(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    runner.invoke(app, ["init", str(repo)])
    assert ".cgir/" in (repo / ".gitignore").read_text()


def test_init_appends_not_clobbers_gitignore(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("node_modules/\n")
    runner.invoke(app, ["init", str(repo)])
    content = (repo / ".gitignore").read_text()
    assert "node_modules/" in content and ".cgir/" in content


def test_init_never_clobbers_existing_config(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "cgir.toml").write_text("# mine\n")
    result = runner.invoke(app, ["init", str(repo)])
    assert result.exit_code == 0
    assert (repo / "cgir.toml").read_text() == "# mine\n"  # untouched


def test_init_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    runner.invoke(app, ["init", str(repo)])
    runner.invoke(app, ["init", str(repo)])
    assert (repo / ".gitignore").read_text().count(".cgir/") == 1


def test_init_hook_flag_installs_seatbelt(tmp_path: Path) -> None:
    repo = _repo(tmp_path, git=True)
    result = runner.invoke(app, ["init", str(repo), "--hook"])
    assert result.exit_code == 0, result.output
    hook = repo / ".git" / "hooks" / "pre-commit"
    assert hook.exists() and "cgir hook run" in hook.read_text()


def test_init_prints_next_steps(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = runner.invoke(app, ["init", str(repo)])
    assert "cgir mcp" in result.output  # agent wiring pointer
