"""Adapter registry — the map from language to adapter, and lookup by file.

Kept separate from ``__init__`` so ``cache.py`` can resolve adapters
without a circular import.
"""

from __future__ import annotations

from cgir.languages.base import LanguageAdapter
from cgir.languages.go import GoAdapter
from cgir.languages.python import PythonAdapter
from cgir.languages.typescript import TypeScriptAdapter

ADAPTERS: dict[str, LanguageAdapter] = {
    a.name: a for a in (PythonAdapter(), TypeScriptAdapter(), GoAdapter())
}

DEFAULT_ADAPTER: LanguageAdapter = ADAPTERS["python"]

_BY_EXTENSION: dict[str, LanguageAdapter] = {
    ext: adapter for adapter in ADAPTERS.values() for ext in adapter.file_extensions
}


def adapter_for_extension(ext: str) -> LanguageAdapter | None:
    """The adapter that claims a file extension (``.py`` → PythonAdapter)."""
    return _BY_EXTENSION.get(ext)
