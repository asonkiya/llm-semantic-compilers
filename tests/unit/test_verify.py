"""RED-phase tests for cgir verify (Sprint 24 — the trust loop).

Contract:

* ``verify(index_dir, component_id, candidate, repo, fail_on=None,
  run_tests=False) -> VerifyResult`` — splice ``candidate`` (a full
  function definition) into a shadow copy of ``repo`` at the component's
  span, rescan, contract-diff the new spec against the indexed one,
  evaluate fail rules. Pure of the caller's repo (works on a copy).
* ``VerifyResult``: ``contract_ok`` (no drift on the target),
  ``violations`` (rule hits), ``drift`` (per-field old/new for the target),
  and — when ``run_tests`` — ``tests_ok`` over the linked test files.
* Unknown component raises ``KeyError``.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from typer.testing import CliRunner

from cgir.cli import app
from cgir.verify import verify


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo).mkdir()
    (repo / "pricing.py").write_text(
        dedent(
            """
            def add_tax(price, rate):
                return price * (1 + rate)
            """
        ).lstrip()
    )
    return repo


def _index(repo: Path, out: Path) -> Path:
    assert CliRunner().invoke(app, ["scan", str(repo), "--out", str(out)]).exit_code == 0
    return out


def test_contract_preserving_candidate_passes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    idx = _index(repo, tmp_path / "idx")
    candidate = "def add_tax(price, rate):\n    total = price * (1 + rate)\n    return total\n"
    result = verify(idx, "pricing.add_tax", candidate, repo)
    assert result.contract_ok
    assert result.violations == []


def test_effect_adding_candidate_flags_drift(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    idx = _index(repo, tmp_path / "idx")
    candidate = "def add_tax(price, rate):\n    print(price)\n    return price * (1 + rate)\n"
    result = verify(idx, "pricing.add_tax", candidate, repo, fail_on=["effect-gain"])
    assert not result.contract_ok
    assert any("io" in v for v in result.violations)
    assert "effects" in result.drift


def test_unknown_component_raises(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    idx = _index(repo, tmp_path / "idx")
    with pytest.raises(KeyError):
        verify(idx, "nope.nothing", "def x(): pass\n", repo)


def test_does_not_mutate_original_repo(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    idx = _index(repo, tmp_path / "idx")
    before = (repo / "pricing.py").read_text()
    verify(idx, "pricing.add_tax", "def add_tax(price, rate):\n    return 0\n", repo)
    assert (repo / "pricing.py").read_text() == before


def test_run_tests_layer(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "test_pricing.py").write_text(
        dedent(
            """
            from pricing import add_tax

            def test_add_tax():
                assert add_tax(100, 0.1) == 110
            """
        ).lstrip()
    )
    idx = _index(repo, tmp_path / "idx")
    # A wrong implementation preserves the contract (still pure) but fails tests.
    wrong = "def add_tax(price, rate):\n    return price\n"
    result = verify(idx, "pricing.add_tax", wrong, repo, run_tests=True)
    assert result.contract_ok  # still a pure function
    assert result.tests_ok is False  # but behaviorally wrong


def test_cli_verify(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    idx = _index(repo, tmp_path / "idx")
    cand = tmp_path / "cand.py"
    cand.write_text("def add_tax(price, rate):\n    print(price)\n    return price\n")
    result = CliRunner().invoke(
        app,
        [
            "verify",
            "pricing.add_tax",
            "--candidate",
            str(cand),
            "--index",
            str(idx),
            "--repo",
            str(repo),
            "--fail-on",
            "effect-gain",
        ],
    )
    assert result.exit_code == 1  # drift → non-zero
    assert "io" in result.output
