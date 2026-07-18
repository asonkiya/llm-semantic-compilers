"""Raise inherits confidence tiers (the rung-3b fix).

An explicit ``raise`` is certain evidence a function can raise — but the
*absence* of one proves nothing (any callee can raise: graphlib's
CycleError, a bare ``d[k]``). Raise-*drift* is therefore never
trustworthy, in either direction: rung 3b measured 2 false rejections and
0 saves from hard-killing on it. The fix: ``raise`` is lexical-tier, so
default gates and verify's contract_ok ignore raise-only drift, while the
``:any`` opt-in and pins still see it. High-confidence effect drift
(io/net/fs/db from known call tables) must keep hard-failing.
"""

from __future__ import annotations

from pathlib import Path

from cgir.export.json_export import read_specs
from cgir.pipeline import scan_repo
from cgir.verify import verify


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "core.py").write_text(
        "def add_tax(price, rate):\n    return price * (1 + rate)\n\n\n"
        "def lookup(table, key):\n"
        "    if key not in table:\n"
        "        raise KeyError(key)\n"
        "    return table[key]\n"
    )
    idx = tmp_path / "idx"
    scan_repo(repo, out=idx)
    return repo, idx


def test_raise_is_lexical_tier_in_specs(tmp_path: Path) -> None:
    _repo(tmp_path)
    specs = {s.id: s for s in read_specs(tmp_path / "idx")}
    spec = specs["pkg.core.lookup"]
    assert "raise" in spec.effects
    assert "raise" in spec.lexical_effects


def test_raise_gain_does_not_fail_verify(tmp_path: Path) -> None:
    """The topo_sort case: candidate raises explicitly where the original
    raised invisibly (or not at all) — report, don't kill."""
    repo, idx = _repo(tmp_path)
    candidate = (
        "def add_tax(price, rate):\n"
        "    if rate < 0:\n"
        "        raise ValueError('negative rate')\n"
        "    return price * (1 + rate)\n"
    )
    result = verify(idx, "pkg.core.add_tax", candidate, repo, run_tests=False)
    assert result.contract_ok
    # transparency: the drift is still visible in the report
    assert "effects" in result.drift


def test_raise_loss_does_not_fail_verify(tmp_path: Path) -> None:
    """The registry.get case: candidate raises via indexing instead of an
    explicit raise — behaviorally equivalent, lexically invisible."""
    repo, idx = _repo(tmp_path)
    candidate = "def lookup(table, key):\n    return table[key]\n"
    result = verify(idx, "pkg.core.lookup", candidate, repo, run_tests=False)
    assert result.contract_ok


def test_raise_drift_opt_in_still_fires(tmp_path: Path) -> None:
    repo, idx = _repo(tmp_path)
    candidate = "def lookup(table, key):\n    return table[key]\n"
    result = verify(
        idx, "pkg.core.lookup", candidate, repo, run_tests=False, fail_on=["effect-loss:any"]
    )
    assert any("lost effect" in v for v in result.violations)


def test_high_confidence_effect_gain_still_fails(tmp_path: Path) -> None:
    repo, idx = _repo(tmp_path)
    candidate = (
        "def add_tax(price, rate):\n"
        "    import requests\n"
        "    requests.post('http://x', json=price)\n"
        "    return price * (1 + rate)\n"
    )
    result = verify(idx, "pkg.core.add_tax", candidate, repo, run_tests=False)
    assert not result.contract_ok
    assert "net" in result.drift.get("effects", {}).get("new", [])


def test_signature_drift_still_fails(tmp_path: Path) -> None:
    repo, idx = _repo(tmp_path)
    candidate = "def add_tax(price, rate, debug):\n    return price * (1 + rate)\n"
    result = verify(idx, "pkg.core.add_tax", candidate, repo, run_tests=False)
    assert not result.contract_ok


def test_no_raise_pin_still_enforced(tmp_path: Path) -> None:
    """Pins are explicit intent — they see lexical-tier tags."""
    repo, idx = _repo(tmp_path)
    candidate = (
        "# cgir: no-raise\n"
        "def add_tax(price, rate):\n"
        "    if rate < 0:\n"
        "        raise ValueError('bad')\n"
        "    return price * (1 + rate)\n"
    )
    result = verify(idx, "pkg.core.add_tax", candidate, repo, run_tests=False)
    assert any("raise" in v for v in result.violations)
