"""The git pre-commit seatbelt: `cgir hook`.

`run_check` scans the committed tree (HEAD) vs the *staged* tree
(``git write-tree``), diffs the contracts, and reports fail-on violations
plus the tests the staged change puts at risk — the local, deterministic
gate on your own (and your agent's) commits. `install` writes the hook.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

from cgir.hooks import install, run_check, uninstall


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(repo),
            "PATH": "/usr/bin:/bin",
        },
    ).strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def _commit(repo: Path, name: str, body: str) -> None:
    (repo / name).write_text(body)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", "x")


def _stage(repo: Path, name: str, body: str) -> None:
    (repo / name).write_text(body)
    _git(repo, "add", name)


PURE = "def add_tax(price, rate):\n    return price * (1 + rate)\n"


def test_body_only_edit_passes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "pricing.py", PURE)
    _stage(
        repo,
        "pricing.py",
        "def add_tax(price, rate):\n    t = price * rate\n    return price + t\n",
    )
    result = run_check(repo)
    assert result.checked is True
    assert result.violations == []


def test_new_network_call_is_blocked(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "pricing.py", PURE)
    _stage(
        repo,
        "pricing.py",
        "import requests\n\ndef add_tax(price, rate):\n    requests.get('http://x')\n    return price * (1 + rate)\n",
    )
    result = run_check(repo)
    assert result.checked is True
    assert any("net" in v for v in result.violations)


def test_no_staged_changes_is_noop(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "pricing.py", PURE)
    result = run_check(repo)
    assert result.checked is False


def test_doc_only_change_skips_scan(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "pricing.py", PURE)
    _stage(repo, "README.md", "# hi\n")
    result = run_check(repo)
    assert result.checked is False  # no supported source files staged


def test_install_writes_executable_hook(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    path = install(repo)
    assert path.exists()
    assert path.stat().st_mode & stat.S_IXUSR
    assert "cgir hook run" in path.read_text()
    assert "effect-gain:net" in path.read_text()


def test_install_refuses_existing_without_force(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    install(repo)
    try:
        install(repo)
    except FileExistsError:
        pass
    else:
        raise AssertionError("expected FileExistsError")
    assert install(repo, force=True).exists()


def test_uninstall_removes_only_our_hook(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    install(repo)
    assert uninstall(repo) is True
    assert uninstall(repo) is False  # already gone
