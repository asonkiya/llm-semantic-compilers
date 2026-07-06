"""Codebase structure report over ComponentSpecs.

``compute_stats`` is a pure function from ``list[ComponentSpec]`` to a
JSON-able dict — it drives both the ``cgir stats`` text output and its
``--json`` mode. Purity buckets follow the scoring rubric in
:mod:`cgir.analyses.purity`: 1.0 pure, 0.7 effect-tainted (calls into
effectful code only), everything below direct-impure.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from cgir.analyses.effects import IMPURE_EFFECT_TAGS
from cgir.ir.component_spec import ComponentSpec

TOP_N = 10
_IMPURE = set(IMPURE_EFFECT_TAGS)


def compute_stats(specs: list[ComponentSpec], top_n: int = TOP_N) -> dict[str, Any]:
    known_ids = {s.id for s in specs}
    files = {s.trace[0].rsplit(":", 1)[0] for s in specs if s.trace}

    kinds = Counter(s.kind.value for s in specs)
    effects = Counter(tag for s in specs for tag in s.effects)

    internal_callers: Counter[str] = Counter()
    external_callers: Counter[str] = Counter()
    constructed: Counter[str] = Counter()
    for s in specs:
        for callee in set(s.calls):
            if callee in known_ids:
                internal_callers[callee] += 1
            else:
                external_callers[callee] += 1
        for type_name in set(s.constructs):
            constructed[type_name] += 1

    pure = sum(1 for s in specs if s.purity == 1.0)
    tainted = sum(1 for s in specs if s.purity == 0.7)
    impure = len(specs) - pure - tainted
    mean = sum(s.purity or 0.0 for s in specs) / len(specs) if specs else 0.0

    fan_out = sorted(specs, key=lambda s: (-len(s.calls), s.id))

    return {
        "total": len(specs),
        "files": len(files),
        "kinds": dict(kinds),
        "purity": {"mean": mean, "pure": pure, "tainted": tainted, "impure": impure},
        "effects": dict(effects),
        "most_called": [
            {"id": callee, "callers": n} for callee, n in internal_callers.most_common(top_n)
        ],
        "top_fan_out": [{"id": s.id, "calls": len(s.calls)} for s in fan_out[:top_n] if s.calls],
        "external_calls": [
            {"id": callee, "callers": n} for callee, n in external_callers.most_common(top_n)
        ],
        "top_constructed": [
            {"id": type_name, "constructors": n} for type_name, n in constructed.most_common(top_n)
        ],
        "entrypoints": [
            {"id": s.id, "entrypoint": s.entrypoint}
            for s in sorted(specs, key=lambda s: (s.entrypoint or "", s.id))
            if s.entrypoint
        ],
        "untested_effectful": [
            {"id": s.id, "effects": s.effects}
            for s in sorted(specs, key=lambda s: s.id)
            if _IMPURE & set(s.effects) and not s.covered_by
        ],
    }


def render_text(stats: dict[str, Any]) -> str:
    """Terminal-friendly rendering of :func:`compute_stats` output."""
    lines: list[str] = []
    lines.append(f"Components: {stats['total']}  (files: {stats['files']})")

    if stats["kinds"]:
        kinds = " · ".join(f"{k} {n}" for k, n in sorted(stats["kinds"].items()))
        lines.append(f"Kinds:      {kinds}")

    purity = stats["purity"]
    lines.append(
        f"Purity:     mean {purity['mean']:.2f} · pure {purity['pure']}"
        f" · tainted {purity['tainted']} · impure {purity['impure']}"
    )

    if stats["effects"]:
        effects = " · ".join(f"{k} {n}" for k, n in sorted(stats["effects"].items()))
        lines.append(f"Effects:    {effects}")

    if stats["entrypoints"]:
        lines.append("Entrypoints:")
        width = max(len(e["entrypoint"]) for e in stats["entrypoints"])
        for entry in stats["entrypoints"]:
            lines.append(f"  {entry['entrypoint']:<{width}}  {entry['id']}")

    if stats["untested_effectful"]:
        lines.append(f"Untested effectful ({len(stats['untested_effectful'])}):")
        for entry in stats["untested_effectful"][:TOP_N]:
            lines.append(f"  [{','.join(entry['effects'])}]  {entry['id']}")

    for title, key, count_key in (
        ("Most called", "most_called", "callers"),
        ("Top fan-out", "top_fan_out", "calls"),
        ("Constructed types", "top_constructed", "constructors"),
        ("External calls", "external_calls", "callers"),
    ):
        entries = stats[key]
        if not entries:
            continue
        lines.append(f"{title}:")
        width = len(str(entries[0][count_key]))
        for entry in entries:
            lines.append(f"  {entry[count_key]:>{width}}x {entry['id']}")

    return "\n".join(lines) + "\n"
