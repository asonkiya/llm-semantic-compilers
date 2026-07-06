"""Language adapters — the per-language seam over a shared analysis pipeline.

Add a language by implementing :class:`LanguageAdapter`; register it in
:data:`ADAPTERS` and everything downstream (stats, viz, flow, diff, pack,
lint, verify) works on it.
"""

from __future__ import annotations

from cgir.languages.base import CallSite, LanguageAdapter
from cgir.languages.cache import SourceCache
from cgir.languages.python import PythonAdapter

ADAPTERS: dict[str, LanguageAdapter] = {a.name: a for a in (PythonAdapter(),)}

DEFAULT_ADAPTER = ADAPTERS["python"]


def adapter_for_extension(ext: str) -> LanguageAdapter | None:
    """The adapter that claims a file extension (``.py`` → PythonAdapter)."""
    for adapter in ADAPTERS.values():
        if ext in adapter.file_extensions:
            return adapter
    return None


__all__ = [
    "ADAPTERS",
    "DEFAULT_ADAPTER",
    "CallSite",
    "LanguageAdapter",
    "PythonAdapter",
    "SourceCache",
    "adapter_for_extension",
]
