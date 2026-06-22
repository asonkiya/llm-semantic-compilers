"""Purity scoring from the effects dict produced by :mod:`cgir.analyses.effects`.

Rubric:
    1.0  no own effects AND only calls into pure components
    0.7  ``calls_effectful`` only (orchestrates effectful callees but does
         no direct IO/raise/state writes itself)
    0.0  any direct effect (io, raise, net, fs, nondeterm)

The future ``state_transformer`` tier (writes module-local state but no IO)
will land alongside the milestone that adds ``WRITES``/``MUTATES`` edges;
for now any effect tag in :data:`effects.DIRECT_EFFECT_TAGS` collapses to 0.0.
"""

from __future__ import annotations

from cgir.analyses.effects import DIRECT_EFFECT_TAGS, TRANSITIVE_TAG
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
        if tags & DIRECT_EFFECT_TAGS:
            scores[node.id] = 0.0
        elif TRANSITIVE_TAG in tags:
            scores[node.id] = 0.7
        else:
            scores[node.id] = 1.0
    return scores
