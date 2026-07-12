"""Ranked, structured component search — the agent front door.

Two query ingredients, freely mixable:

* **free terms** rank by where they hit: exact component name (100) >
  name-word (50) > id substring (20) > entrypoint substring (15) > doc
  substring (5); scores sum across terms.
* **predicates** (``key:value``) filter on the *contract* — the queries a
  vector index can't answer: ``kind:pure effects:net lexical:false
  callers:>3 pins:pure entrypoint:HTTP lang:go covered:false``.

Pure over ComponentSpecs; drives both the MCP ``search`` tool and the
``cgir search`` CLI.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from cgir.ir.component_spec import ComponentSpec

MAX_RESULTS = 25

_KIND_ALIASES = {"pure": "pure_function", "adapter": "effect_adapter"}


def search_specs(specs: list[ComponentSpec], query: str) -> list[ComponentSpec]:
    """Specs matching ``query``, best-ranked first (capped at MAX_RESULTS)."""
    terms: list[str] = []
    predicates: list[Callable[[ComponentSpec], bool]] = []
    caller_counts: Counter[str] | None = None

    for token in query.split():
        key, _, value = token.partition(":")
        if value and key in _PREDICATES:
            if key == "callers" and caller_counts is None:
                caller_counts = Counter(c for s in specs for c in s.calls)
            predicates.append(_PREDICATES[key](value, caller_counts))
        else:
            terms.append(token.lower())

    if not terms and not predicates:
        return []

    scored: list[tuple[float, str, ComponentSpec]] = []
    for spec in specs:
        if not all(pred(spec) for pred in predicates):
            continue
        score = _score(spec, terms)
        if terms and score == 0:
            continue
        scored.append((-score, spec.id, spec))
    scored.sort()
    return [spec for _, _, spec in scored[:MAX_RESULTS]]


def _score(spec: ComponentSpec, terms: list[str]) -> float:
    if not terms:
        return 0.0
    name = spec.id.rsplit(".", 1)[-1].lower()
    spec_id = spec.id.lower()
    entrypoint = (spec.entrypoint or "").lower()
    doc = (spec.doc or "").lower()
    total = 0.0
    for term in terms:
        if term == name:
            total += 100
        elif term in name:
            total += 50
        elif term in spec_id:
            total += 20
        elif term in entrypoint:
            total += 15
        elif term in doc:
            total += 5
    return total


# --- predicates ------------------------------------------------------------------


def _number_test(value: str) -> Callable[[int], bool]:
    if value.startswith(">"):
        threshold = int(value[1:])
        return lambda n: n > threshold
    if value.startswith("<"):
        threshold = int(value[1:])
        return lambda n: n < threshold
    exact = int(value)
    return lambda n: n == exact


def _p_kind(value: str, _counts: Counter[str] | None) -> Callable[[ComponentSpec], bool]:
    wanted = _KIND_ALIASES.get(value, value)
    return lambda s: s.kind.value == wanted


def _p_effects(value: str, _counts: Counter[str] | None) -> Callable[[ComponentSpec], bool]:
    if value == "none":
        return lambda s: not s.effects
    return lambda s: value in s.effects


def _p_lexical(value: str, _counts: Counter[str] | None) -> Callable[[ComponentSpec], bool]:
    want = value == "true"
    return lambda s: bool(s.lexical_effects) == want


def _p_callers(value: str, counts: Counter[str] | None) -> Callable[[ComponentSpec], bool]:
    test = _number_test(value)
    lookup = counts or Counter()
    return lambda s: test(lookup.get(s.id, 0))


def _p_calls(value: str, _counts: Counter[str] | None) -> Callable[[ComponentSpec], bool]:
    test = _number_test(value)
    return lambda s: test(len(s.calls))


def _p_pins(value: str, _counts: Counter[str] | None) -> Callable[[ComponentSpec], bool]:
    return lambda s: value in s.pins


def _p_pinned(value: str, _counts: Counter[str] | None) -> Callable[[ComponentSpec], bool]:
    want = value == "true"
    return lambda s: bool(s.pins) == want


def _p_entrypoint(value: str, _counts: Counter[str] | None) -> Callable[[ComponentSpec], bool]:
    if value in {"true", "false"}:
        want = value == "true"
        return lambda s: bool(s.entrypoint) == want
    return lambda s: value.lower() in (s.entrypoint or "").lower()


def _p_lang(value: str, _counts: Counter[str] | None) -> Callable[[ComponentSpec], bool]:
    return lambda s: (s.language or "") == value


def _p_covered(value: str, _counts: Counter[str] | None) -> Callable[[ComponentSpec], bool]:
    want = value == "true"
    return lambda s: bool(s.covered_by) == want


_PREDICATES: dict[str, Callable[[str, Counter[str] | None], Callable[[ComponentSpec], bool]]] = {
    "kind": _p_kind,
    "effects": _p_effects,
    "lexical": _p_lexical,
    "callers": _p_callers,
    "calls": _p_calls,
    "pins": _p_pins,
    "pinned": _p_pinned,
    "entrypoint": _p_entrypoint,
    "lang": _p_lang,
    "covered": _p_covered,
}


def render_search(specs: list[ComponentSpec], query: str) -> str:
    hits = search_specs(specs, query)
    if not hits:
        return f"no components match {query!r}\n"
    lines: list[str] = []
    for spec in hits:
        parts = [spec.id, f"[{spec.kind.value}]"]
        if spec.effects:
            lexical = set(spec.lexical_effects)
            parts.append(
                "effects: " + ",".join(t + "?" if t in lexical else t for t in spec.effects)
            )
        if spec.pins:
            parts.append(f"pinned: {','.join(spec.pins)}")
        if spec.entrypoint:
            parts.append(f"({spec.entrypoint})")
        lines.append("  ".join(parts))
    if len(hits) == MAX_RESULTS:
        lines.append(f"(capped at {MAX_RESULTS} — refine the query)")
    return "\n".join(lines) + "\n"


__all__ = ["MAX_RESULTS", "render_search", "search_specs"]
