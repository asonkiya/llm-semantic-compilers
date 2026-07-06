"""Parse-once source cache, keyed by repo-relative path.

Analysis passes walk *per function*, so without this a file with N
functions gets parsed N times per pass. Each file is parsed once with the
adapter that claims its extension (or a forced adapter, for single-language
callers / tests).
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node as TSNode

from cgir.languages.base import LanguageAdapter
from cgir.languages.registry import adapter_for_extension


class SourceCache:
    def __init__(self, repo_path: Path, adapter: LanguageAdapter | None = None) -> None:
        self._repo_path = repo_path
        self._forced = adapter
        self._entries: dict[str, tuple[bytes, TSNode, LanguageAdapter] | None] = {}

    def get(self, rel_path: str) -> tuple[bytes, TSNode, LanguageAdapter] | None:
        """Return ``(source_bytes, root_node, adapter)`` for a path, or None."""
        if rel_path not in self._entries:
            adapter = self._forced or adapter_for_extension(Path(rel_path).suffix)
            if adapter is None:
                self._entries[rel_path] = None
            else:
                try:
                    source = (self._repo_path / rel_path).read_bytes()
                except OSError:
                    self._entries[rel_path] = None
                else:
                    self._entries[rel_path] = (source, adapter.parse(source), adapter)
        return self._entries[rel_path]
