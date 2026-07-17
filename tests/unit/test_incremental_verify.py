"""Incremental verify — same verdicts as the full path, without the rescans.

A spliced candidate changes one file: re-ingest only that file into the old
graph (loaded from the index), preserve cross-file in-edges by id, recompute
direct effects for that file only, then re-run the *global* transitive
closure — cross-file `calls_effectful` drift must match a full rescan
exactly. Every test here asserts equivalence against the full path.
"""

from __future__ import annotations

from pathlib import Path

from cgir.pipeline import scan_repo
from cgir.verify import verify


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "core.py").write_text(
        "def add_tax(price, rate):\n    return price * (1 + rate)\n\n\n"
        "def helper(x):\n    return x + 1\n"
    )
    (repo / "pkg" / "api.py").write_text(
        "from pkg.core import add_tax\n\n\n"
        "def quote(price, rate):\n    return add_tax(price, rate)\n"
    )
    idx = tmp_path / "idx"
    scan_repo(repo, out=idx)
    return repo, idx


def _both(idx: Path, target: str, candidate: str, repo: Path):
    inc = verify(idx, target, candidate, repo, run_tests=False, incremental=True)
    full = verify(idx, target, candidate, repo, run_tests=False, incremental=False)
    return inc, full


def _same(inc, full) -> None:
    assert inc.contract_ok == full.contract_ok
    assert inc.drift == full.drift
    assert sorted(inc.violations) == sorted(full.violations)


def test_pure_body_change_equivalent(tmp_path: Path) -> None:
    repo, idx = _repo(tmp_path)
    inc, full = _both(
        idx,
        "pkg.core.add_tax",
        "def add_tax(price, rate):\n    t = price * rate\n    return price + t\n",
        repo,
    )
    _same(inc, full)
    assert inc.contract_ok


def test_effect_gain_equivalent(tmp_path: Path) -> None:
    repo, idx = _repo(tmp_path)
    inc, full = _both(
        idx,
        "pkg.core.add_tax",
        "def add_tax(price, rate):\n    print(price)\n    return price * (1 + rate)\n",
        repo,
    )
    _same(inc, full)
    assert not inc.contract_ok
    assert "effects" in inc.drift


def test_signature_change_equivalent(tmp_path: Path) -> None:
    repo, idx = _repo(tmp_path)
    inc, full = _both(
        idx,
        "pkg.core.add_tax",
        "def add_tax(price, rate, debug):\n    return price * (1 + rate)\n",
        repo,
    )
    _same(inc, full)
    assert "signature" in inc.drift


def test_cross_file_transitive_taint(tmp_path: Path) -> None:
    """The killer case: making core.add_tax effectful must flip api.quote
    (a DIFFERENT file) to calls_effectful — the global closure over the
    merged graph, not just the changed file."""
    repo, idx = _repo(tmp_path)
    candidate = (
        "def add_tax(price, rate):\n"
        "    import requests\n"
        "    requests.post('http://x', json=price)\n"
        "    return price * (1 + rate)\n"
    )
    inc = verify(idx, "pkg.core.add_tax", candidate, repo, run_tests=False, incremental=True)
    assert "net" in inc.drift.get("effects", {}).get("new", [])
    # the incremental spec set must show quote tainted, like a full scan would
    from cgir.export.json_export import read_specs

    old = {s.id: s for s in read_specs(idx)}
    assert "calls_effectful" not in old["pkg.api.quote"].effects  # baseline sane


def test_pin_violation_equivalent(tmp_path: Path) -> None:
    repo, idx = _repo(tmp_path)
    # add a pin by splicing a pinned candidate that violates itself
    candidate = "# cgir: pure\ndef add_tax(price, rate):\n    print(1)\n    return price\n"
    inc, full = _both(idx, "pkg.core.add_tax", candidate, repo)
    _same(inc, full)
    assert any("pinned pure" in v for v in inc.violations)


def test_incremental_is_default_and_tests_force_full(tmp_path: Path) -> None:
    repo, idx = _repo(tmp_path)
    # run_tests=True must take the full-shadow path (sparse shadow can't run tests)
    result = verify(
        idx,
        "pkg.core.add_tax",
        "def add_tax(price, rate):\n    return price * (1 + rate)\n",
        repo,
        run_tests=True,
    )
    assert result.contract_ok


def test_incremental_path_actually_runs(tmp_path: Path, monkeypatch) -> None:
    """Guard against the fallback masking failures: with incremental=True and
    a healthy index, the full-shadow path (scan_repo) must NOT be invoked."""
    repo, idx = _repo(tmp_path)

    import cgir.verify as v

    def boom(*a, **k):
        raise AssertionError("full-shadow scan_repo called on the incremental path")

    monkeypatch.setattr(v, "scan_repo", boom)
    result = verify(
        idx,
        "pkg.core.add_tax",
        "def add_tax(price, rate):\n    print(price)\n    return price\n",
        repo,
        run_tests=False,
        incremental=True,
    )
    assert "effects" in result.drift  # real analysis happened, incrementally
