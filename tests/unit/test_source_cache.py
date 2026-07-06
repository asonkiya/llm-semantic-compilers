"""Tests for the parse-once SourceCache (cgir.languages.cache)."""

from __future__ import annotations

from pathlib import Path

from cgir.languages import PythonAdapter, SourceCache


def test_source_cache_parses_each_file_once(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("def f():\n    pass\n")
    cache = SourceCache(tmp_path, PythonAdapter())
    first = cache.get("m.py")
    second = cache.get("m.py")
    assert first is not None
    assert first is second, "repeated lookups must return the cached parse"


def test_source_cache_returns_source_and_root(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("x = 1\n")
    cache = SourceCache(tmp_path, PythonAdapter())
    parsed = cache.get("m.py")
    assert parsed is not None
    source, root, _ = parsed
    assert source == b"x = 1\n"
    assert root.type == "module"


def test_source_cache_missing_file_is_none_and_cached(tmp_path: Path) -> None:
    cache = SourceCache(tmp_path, PythonAdapter())
    assert cache.get("nope.py") is None
    assert cache.get("nope.py") is None
