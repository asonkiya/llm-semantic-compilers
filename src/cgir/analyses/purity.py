"""Purity scoring from the effects dict produced by :mod:`cgir.analyses.effects`.

Rubric:
    1.0  no own impure effects AND only calls into pure components
    0.7  ``calls_effectful`` only (orchestrates effectful callees but does
         no direct IO/state writes itself)
    0.0  any direct impure effect (io, net, fs, nondeterm, db)

``raise`` is *not* impure (settled Sprint 13): exceptions are control flow,
so a raise-only validator scores 1.0 while keeping the ``raise`` tag in its
effects list.
"""

from __future__ import annotations

from cgir.analyses.effects import IMPURE_EFFECT_TAGS, TRANSITIVE_TAG
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import NodeKind

# Retained so callers that compute specs without a purity pass still get a
# defined value; downstream code can treat this as "no information".
PLACEHOLDER_SCORE = 0.5


def score(graph: RepoGraph, effects: dict[str, list[str]]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for node in graph.nodes():
        if node.kind not in {NodeKind.Function, NodeKind.Method}:
            continue
        tags = set(effects.get(node.id, []))
        if tags & IMPURE_EFFECT_TAGS:
            scores[node.id] = 0.0
        elif TRANSITIVE_TAG in tags:
            scores[node.id] = 0.7
        else:
            scores[node.id] = 1.0
    return scores
