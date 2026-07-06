"""Parse-once source cache, keyed by repo-relative path.

Analysis passes walk *per function*, so without this a file with N
functions gets parsed N times per pass. Parses each file at most once
using the active :class:`~cgir.languages.base.LanguageAdapter`.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node as TSNode

from cgir.languages.base import LanguageAdapter


class SourceCache:
    def __init__(self, adapter: LanguageAdapter, repo_path: Path) -> None:
        self._adapter = adapter
        self._repo_path = repo_path
        self._entries: dict[str, tuple[bytes, TSNode] | None] = {}

    def get(self, rel_path: str) -> tuple[bytes, TSNode] | None:
        """Return ``(source_bytes, root_node)`` for a repo-relative path, or None."""
        if rel_path not in self._entries:
            try:
                source = (self._repo_path / rel_path).read_bytes()
            except OSError:
                self._entries[rel_path] = None
            else:
                self._entries[rel_path] = (source, self._adapter.parse(source))
        return self._entries[rel_path]
