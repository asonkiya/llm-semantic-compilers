"""Adapter registry — builtins plus third-party language plugins.

A plugin is a package with an entry point in the ``cgir.languages`` group:

    [project.entry-points."cgir.languages"]
    rust = "cgir_rust:RustAdapter"

``pip install cgir-rust`` is the whole integration. Safety rules: builtins
win extension conflicts, a broken plugin degrades to a warning (never a
crash), and an adapter-API version mismatch warns but still loads (the ABC
gives new optional methods defaults). Kept separate from ``__init__`` so
``cache.py`` can resolve adapters without a circular import.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from importlib import metadata
from typing import Any

from cgir.languages.base import ADAPTER_API_VERSION, LanguageAdapter
from cgir.languages.go import GoAdapter
from cgir.languages.python import PythonAdapter
from cgir.languages.rust import RustAdapter
from cgir.languages.typescript import TypeScriptAdapter

_BUILTINS: tuple[LanguageAdapter, ...] = (
    PythonAdapter(),
    TypeScriptAdapter(),
    GoAdapter(),
    RustAdapter(),
)


def discover_adapters(
    entry_points: Iterable[Any],
) -> tuple[dict[str, LanguageAdapter], list[str]]:
    """Load plugin adapters from entry points: ``(adapters, warnings)``.

    Starts from the builtins; each entry point may add one adapter. A plugin
    is rejected (with a warning, never an exception) when it fails to load,
    is not a LanguageAdapter, reuses a language name, or claims an extension
    a builtin owns.
    """
    adapters: dict[str, LanguageAdapter] = {a.name: a for a in _BUILTINS}
    claimed: set[str] = {ext for a in _BUILTINS for ext in a.file_extensions}
    notes: list[str] = []

    for ep in entry_points:
        ep_name = getattr(ep, "name", "<unknown>")
        try:
            obj = ep.load()
            adapter = obj() if isinstance(obj, type) else obj
        except Exception as exc:  # a hostile/broken plugin must not crash cgir
            notes.append(f"language plugin {ep_name!r} failed to load: {exc}")
            continue
        if not isinstance(adapter, LanguageAdapter):
            notes.append(f"language plugin {ep_name!r} is not a LanguageAdapter — skipped")
            continue
        if adapter.name in adapters:
            notes.append(
                f"language plugin {ep_name!r} reuses language name {adapter.name!r} — skipped"
            )
            continue
        conflicts = sorted(set(adapter.file_extensions) & claimed)
        if conflicts:
            notes.append(
                f"language plugin {ep_name!r} claims already-owned extension(s) "
                f"{', '.join(conflicts)} — skipped"
            )
            continue
        declared = getattr(adapter, "api_version", ADAPTER_API_VERSION)
        if declared != ADAPTER_API_VERSION:
            notes.append(
                f"language plugin {ep_name!r} declares adapter api version {declared}, "
                f"cgir provides {ADAPTER_API_VERSION} — loading anyway (defaults apply)"
            )
        adapters[adapter.name] = adapter
        claimed.update(adapter.file_extensions)
    return adapters, notes


def _installed_entry_points() -> Iterable[Any]:
    try:
        return metadata.entry_points(group="cgir.languages")
    except Exception:
        return []


ADAPTERS, _PLUGIN_WARNINGS = discover_adapters(_installed_entry_points())
for _note in _PLUGIN_WARNINGS:
    warnings.warn(_note, stacklevel=2)

DEFAULT_ADAPTER: LanguageAdapter = ADAPTERS["python"]

_BY_EXTENSION: dict[str, LanguageAdapter] = {
    ext: adapter for adapter in ADAPTERS.values() for ext in adapter.file_extensions
}


def adapter_for_extension(ext: str) -> LanguageAdapter | None:
    """The adapter that claims a file extension (``.py`` → PythonAdapter)."""
    return _BY_EXTENSION.get(ext)
