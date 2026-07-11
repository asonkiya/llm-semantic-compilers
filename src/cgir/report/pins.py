"""Pin enforcement — developer-declared invariants from ``cgir:`` pragmas.

Pins are the *intent* layer over the inferred contracts: ``# cgir: pure``
above a function means "this must stay pure," and the pipeline holds you
(and your agent) to it. Two classes, two checkers, both pure over specs:

* :func:`state_violations` — ``pure`` / ``no-<tag>``: hold on every scan,
  including newly added code. ``pure`` requires the pure_function kind and no
  impure effect anywhere in the transitive callee closure; ``no-<tag>``
  requires the tag absent from the transitive closure (computed over
  ``spec.calls``, so it is as strong as call resolution).
* :func:`change_violations` — ``stable-signature`` / ``frozen``: hold across
  a scan pair. Always evaluated — the pin is the opt-in, no ``--fail-on``
  needed. Pins are read from the *new* side (the pin lives in the code being
  committed); a pinned component's removal is itself a violation.

Honesty note: a pin is only as strong as effect detection — the lexical
escapes documented in ``docs/status.md`` apply to pins too.
"""

from __future__ import annotations

from cgir.analyses.effects import IMPURE_EFFECT_TAGS, TRANSITIVE_TAG
from cgir.ir.component_spec import ComponentKind, ComponentSpec

STATE_PINS = frozenset({"pure", "no-net", "no-fs", "no-db", "no-io", "no-nondeterm", "no-raise"})
CHANGE_PINS = frozenset({"stable-signature", "frozen"})
KNOWN_PINS = STATE_PINS | CHANGE_PINS

_FROZEN_FIELDS = ("kind", "effects", "purity", "signature", "outputs")


def _transitive_effects(specs: list[ComponentSpec]) -> dict[str, set[str]]:
    """Effect tags reachable from each component via ``spec.calls`` closure."""
    by_id = {s.id: s for s in specs}
    memo: dict[str, set[str]] = {}

    def visit(spec_id: str, seen: set[str]) -> set[str]:
        if spec_id in memo:
            return memo[spec_id]
        if spec_id in seen:
            return set()  # cycle — contributes nothing new on this path
        seen.add(spec_id)
        spec = by_id[spec_id]
        tags = set(spec.effects)
        for callee in spec.calls:
            if callee in by_id:
                tags |= visit(callee, seen)
        memo[spec_id] = tags
        return tags

    for spec in specs:
        visit(spec.id, set())
    return memo


def state_violations(specs: list[ComponentSpec]) -> list[str]:
    """Violations of single-scan pins (``pure``, ``no-<tag>``) + unknown pins."""
    pinned = [s for s in specs if s.pins]
    if not pinned:
        return []
    reach = _transitive_effects(specs)
    found: list[str] = []
    for spec in pinned:
        tags = reach.get(spec.id, set(spec.effects))
        for pin in spec.pins:
            if pin == "pure":
                impure = sorted((tags & IMPURE_EFFECT_TAGS) | (tags & {TRANSITIVE_TAG}))
                if spec.kind is not ComponentKind.pure_function or impure:
                    detail = f"effects {impure}" if impure else f"kind {spec.kind.value}"
                    found.append(f"{spec.id}: pinned pure but {detail}")
            elif pin.startswith("no-") and pin in STATE_PINS:
                tag = pin.removeprefix("no-")
                if tag in tags:
                    where = "directly" if tag in spec.effects else "transitively"
                    found.append(f"{spec.id}: pinned {pin} but reaches {tag} {where}")
            elif pin not in KNOWN_PINS:
                found.append(f"{spec.id}: unknown pin {pin!r}")
    return found


def change_violations(old_specs: list[ComponentSpec], new_specs: list[ComponentSpec]) -> list[str]:
    """Violations of scan-pair pins (``stable-signature``, ``frozen``)."""
    old = {s.id: s for s in old_specs}
    new = {s.id: s for s in new_specs}
    found: list[str] = []

    for spec_id in sorted(old.keys() - new.keys()):
        pins = set(old[spec_id].pins) & CHANGE_PINS
        if pins:
            found.append(f"{spec_id}: pinned {', '.join(sorted(pins))} but removed")

    for spec_id in sorted(old.keys() & new.keys()):
        before, after = old[spec_id], new[spec_id]
        pins = set(after.pins)  # the pin lives in the code being committed
        if "stable-signature" in pins:
            for field in ("signature", "outputs"):
                if getattr(before, field) != getattr(after, field):
                    found.append(
                        f"{spec_id}: pinned stable-signature but {field} changed "
                        f"{getattr(before, field)!r} -> {getattr(after, field)!r}"
                    )
        if "frozen" in pins:
            changed = [
                f for f in _FROZEN_FIELDS if _plain(getattr(before, f)) != _plain(getattr(after, f))
            ]
            if changed:
                found.append(f"{spec_id}: pinned frozen but changed {', '.join(changed)}")
    return found


def _plain(value: object) -> object:
    return value.value if isinstance(value, ComponentKind) else value


__all__ = ["CHANGE_PINS", "KNOWN_PINS", "STATE_PINS", "change_violations", "state_violations"]
