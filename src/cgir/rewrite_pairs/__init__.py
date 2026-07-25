"""Rewrite pairs — the per-(source→target) seam over one shared rewrite engine.

Add a pair by subclassing :class:`SearchLoopPair` (implement worklist /
build_prompt / evaluate) and registering it via the ``cgir.rewrite_pairs``
entry-point group; the search loop, ledger, budget, escalation, and reporting
are inherited. See ``docs/design-rewrite-pairs.md``.
"""

from __future__ import annotations

from cgir.rewrite_pairs.base import (
    FunctionPair,
    PairContext,
    RewritePair,
    SearchLoopPair,
)
from cgir.rewrite_pairs.registry import (
    REWRITE_PAIRS,
    available_pairs,
    discover_pairs,
    pair_for,
)

__all__ = [
    "REWRITE_PAIRS",
    "FunctionPair",
    "PairContext",
    "RewritePair",
    "SearchLoopPair",
    "available_pairs",
    "discover_pairs",
    "pair_for",
]
