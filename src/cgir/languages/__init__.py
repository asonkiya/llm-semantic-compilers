"""Language adapters — the per-language seam over a shared analysis pipeline.

Add a language by implementing :class:`LanguageAdapter`; register it in
``registry.ADAPTERS`` and everything downstream (stats, viz, flow, diff,
pack, lint, verify) works on it.
"""

from __future__ import annotations

from cgir.languages.base import CallSite, LanguageAdapter
from cgir.languages.cache import SourceCache
from cgir.languages.python import PythonAdapter
from cgir.languages.registry import ADAPTERS, DEFAULT_ADAPTER, adapter_for_extension
from cgir.languages.typescript import TypeScriptAdapter

__all__ = [
    "ADAPTERS",
    "DEFAULT_ADAPTER",
    "CallSite",
    "LanguageAdapter",
    "PythonAdapter",
    "SourceCache",
    "TypeScriptAdapter",
    "adapter_for_extension",
]
