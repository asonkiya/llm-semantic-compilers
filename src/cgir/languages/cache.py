"""Parse-once source cache.

Two layers of caching, both keyed so they are always correct:

* :func:`parse_cached` — a process-wide store keyed by ``(adapter, content
  hash)``. A parse tree is a pure function of the bytes and the grammar, so
  this is safe across analysis passes *and* across repeated scans (watch
  mode): an unchanged file is parsed once, ever. Bounded LRU so a long-lived
  watch process doesn't grow without limit.
* :class:`SourceCache` — a per-run, path-keyed view used by the analysis
  passes, which walk *per function* and would otherwise re-read/re-parse a
  file once per function. It now resolves parse trees through
  :func:`parse_cached`, so the ingest pass and every analysis share one
  parse per content.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from pathlib import Path

from tree_sitter import Node as TSNode

from cgir.languages.base import LanguageAdapter
from cgir.languages.registry import adapter_for_extension

# (adapter name, sha256(content)) -> parsed root. Bounded LRU.
_PARSE_STORE: OrderedDict[tuple[str, bytes], TSNode] = OrderedDict()
_PARSE_STORE_MAX = 8192


def parse_cached(adapter: LanguageAdapter, source: bytes) -> TSNode:
    """Parse ``source`` with ``adapter``, reusing a cached tree for identical
    content. Correct across passes and scans — the tree only depends on the
    grammar and the bytes."""
    key = (adapter.name, hashlib.sha256(source).digest())
    cached = _PARSE_STORE.get(key)
    if cached is not None:
        _PARSE_STORE.move_to_end(key)
        return cached
    root = adapter.parse(source)
    _PARSE_STORE[key] = root
    if len(_PARSE_STORE) > _PARSE_STORE_MAX:
        _PARSE_STORE.popitem(last=False)
    return root


def clear_parse_cache() -> None:
    """Drop the process-wide parse store (tests / memory reclamation)."""
    _PARSE_STORE.clear()


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
                    self._entries[rel_path] = (source, parse_cached(adapter, source), adapter)
        return self._entries[rel_path]
