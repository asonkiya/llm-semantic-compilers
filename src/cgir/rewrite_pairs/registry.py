"""Rewrite-pair registry — builtins plus third-party plugins.

A plugin is a package with an entry point in the ``cgir.rewrite_pairs`` group:

    [project.entry-points."cgir.rewrite_pairs"]
    python-python = "cgir_refactor:PythonPythonPair"

``pip install cgir-refactor`` is the whole integration. Safety rules mirror
:mod:`cgir.languages.registry`: builtins win name conflicts, a broken plugin
degrades to a warning (never a crash), an API-version mismatch warns but still
loads (the base class supplies defaults for new methods).
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from importlib import metadata
from typing import Any

from cgir.rewrite_pairs.base import PAIR_API_VERSION, RewritePair
from cgir.rewrite_pairs.builtins import BUILTIN_PAIRS


def discover_pairs(entry_points: Iterable[Any]) -> tuple[dict[str, RewritePair], list[str]]:
    """Load plugin pairs from entry points: ``(pairs, warnings)``. Starts from
    the builtins; each entry point may add one pair. A plugin is rejected (with
    a warning, never an exception) when it fails to load, isn't a RewritePair,
    or reuses a name a builtin owns."""
    pairs: dict[str, RewritePair] = {p.name: p for p in BUILTIN_PAIRS}
    builtin_names = set(pairs)
    notes: list[str] = []

    for ep in entry_points:
        ep_name = getattr(ep, "name", "<unknown>")
        try:
            obj = ep.load()
            pair = obj() if isinstance(obj, type) else obj
        except Exception as exc:  # a hostile/broken plugin must not crash cgir
            notes.append(f"rewrite-pair plugin {ep_name!r} failed to load: {exc}")
            continue
        if not isinstance(pair, RewritePair):
            notes.append(
                f"rewrite-pair plugin {ep_name!r} is not a RewritePair (needs name/description/run)"
                " — skipped"
            )
            continue
        if pair.name in builtin_names:
            notes.append(
                f"rewrite-pair plugin {ep_name!r} reuses builtin name {pair.name!r} — skipped"
            )
            continue
        declared = getattr(pair, "api_version", PAIR_API_VERSION)
        if declared != PAIR_API_VERSION:
            notes.append(
                f"rewrite-pair plugin {ep_name!r} declares pair api version {declared}, "
                f"cgir provides {PAIR_API_VERSION} — loading anyway (defaults apply)"
            )
        pairs[pair.name] = pair
    return pairs, notes


def _installed_entry_points() -> Iterable[Any]:
    try:
        return metadata.entry_points(group="cgir.rewrite_pairs")
    except Exception:
        return []


REWRITE_PAIRS, _PLUGIN_WARNINGS = discover_pairs(_installed_entry_points())
for _note in _PLUGIN_WARNINGS:
    warnings.warn(_note, stacklevel=2)


def pair_for(name: str) -> RewritePair | None:
    """The pair registered under ``name`` (``"c-rust"``), or None."""
    return REWRITE_PAIRS.get(name)


def available_pairs() -> list[str]:
    """Sorted names of every registered pair — for ``--help`` and error text."""
    return sorted(REWRITE_PAIRS)
