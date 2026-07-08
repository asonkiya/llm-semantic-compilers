"""Watch mode: keep the index fresh and report live contract drift.

The pure pieces — content hashing, change detection, and one ``tick`` of
the loop — are unit-tested here; the blocking poll loop is a thin wrapper
over ``tick``.
"""

from __future__ import annotations

from pathlib import Path

from cgir.watch import diff_hashes, read_manifest, source_hashes, tick


def _repo(tmp_path: Path, **files: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, body in files.items():
        (repo / name).write_text(body)
    return repo


def test_source_hashes_covers_only_supported_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path, **{"a.py": "x=1\n", "README.md": "# hi\n"})
    hashes = source_hashes(repo)
    assert "a.py" in hashes
    assert "README.md" not in hashes


def test_source_hashes_skips_ignored_dirs(tmp_path: Path) -> None:
    repo = _repo(tmp_path, **{"a.py": "x=1\n"})
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "b.ts").write_text("const x = 1\n")
    assert "node_modules/b.ts" not in source_hashes(repo)


def test_diff_hashes_classifies_changes() -> None:
    old = {"a.py": "1", "b.py": "1", "gone.py": "1"}
    new = {"a.py": "1", "b.py": "2", "new.py": "1"}
    changed, added, deleted = diff_hashes(old, new)
    assert changed == ["b.py"]
    assert added == ["new.py"]
    assert deleted == ["gone.py"]


def test_tick_no_change_does_not_reindex(tmp_path: Path) -> None:
    repo = _repo(tmp_path, **{"pricing.py": "def add_tax(p, r):\n    return p * (1 + r)\n"})
    index = tmp_path / "idx"
    from cgir.pipeline import scan_repo

    scan_repo(repo, out=index)
    result, _ = tick(repo, index, source_hashes(repo))
    assert result.reindexed is False
    assert result.drift is None


def test_tick_reports_contract_drift_on_change(tmp_path: Path) -> None:
    repo = _repo(tmp_path, **{"pricing.py": "def add_tax(p, r):\n    return p * (1 + r)\n"})
    index = tmp_path / "idx"
    from cgir.pipeline import scan_repo

    scan_repo(repo, out=index)
    prev = source_hashes(repo)
    # an agent makes the pure function hit the network
    (repo / "pricing.py").write_text(
        "import requests\n\ndef add_tax(p, r):\n    requests.get('http://x')\n    return p * (1 + r)\n"
    )
    result, new_hashes = tick(repo, index, prev)
    assert result.reindexed is True
    assert result.changed == ["pricing.py"]
    assert result.drift is not None
    gained = [c for c in result.drift["changed"] if "effects" in c["fields"]]
    assert gained and gained[0]["fields"]["effects"]["new"] == ["net"]
    assert new_hashes != prev


def test_tick_persists_manifest(tmp_path: Path) -> None:
    repo = _repo(tmp_path, **{"a.py": "x = 1\n"})
    index = tmp_path / "idx"
    from cgir.pipeline import scan_repo

    scan_repo(repo, out=index)
    tick(repo, index, {})  # empty prev -> treats a.py as added -> reindex + write manifest
    assert read_manifest(index).get("a.py")
