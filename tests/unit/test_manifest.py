"""RED-phase tests for the index manifest (Sprint 20).

Contract:

* ``write_manifest(index_dir, component_count) -> Manifest`` writes
  ``manifest.json`` with ``cgir_version``, ``schema_version``,
  ``component_count``, ``created_at`` (ISO-8601 UTC).
* ``read_manifest(index_dir)`` round-trips it; returns None if absent
  (old indexes predate the manifest — degrade, don't crash).
* ``write_index`` emits a manifest as a side effect.
"""

from __future__ import annotations

import json
from pathlib import Path

from cgir import __version__
from cgir.ir.component_spec import ComponentKind, ComponentSpec
from cgir.ir.graph import RepoGraph
from cgir.manifest import SCHEMA_VERSION, read_manifest, write_manifest


def test_write_manifest_round_trips(tmp_path: Path) -> None:
    written = write_manifest(tmp_path, component_count=7)
    assert written.cgir_version == __version__
    assert written.schema_version == SCHEMA_VERSION
    assert written.component_count == 7

    loaded = read_manifest(tmp_path)
    assert loaded is not None
    assert loaded.cgir_version == __version__
    assert loaded.component_count == 7
    assert loaded.created_at  # non-empty ISO timestamp


def test_read_manifest_absent_returns_none(tmp_path: Path) -> None:
    assert read_manifest(tmp_path) is None


def test_manifest_file_is_json(tmp_path: Path) -> None:
    write_manifest(tmp_path, component_count=3)
    data = json.loads((tmp_path / "manifest.json").read_text())
    assert set(data) >= {"cgir_version", "schema_version", "component_count", "created_at"}


def test_write_index_emits_manifest(tmp_path: Path) -> None:
    from cgir.export.json_export import write_index

    graph = RepoGraph()
    spec = ComponentSpec(id="m.f", kind=ComponentKind.pure_function, trace=["m.py:1"])
    write_index(tmp_path, graph, [spec])
    manifest = read_manifest(tmp_path)
    assert manifest is not None
    assert manifest.component_count == 1
