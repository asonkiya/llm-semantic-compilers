"""Index diff — architecture drift between two scans (Sprint 16).

``compute_diff`` is pure over two spec lists (same pattern as
:mod:`cgir.report.stats`): which components appeared, disappeared, or
changed on the *contract* fields — kind, purity, effects, signature,
outputs. ``violations`` evaluates CI fail rules against a diff, so
"this PR made a pure function effectful" can fail a build:

    cgir diff old-index new-index --fail-on effect-gain:net --fail-on purity-drop

Rules only fire for components present in *both* scans — new effectful
code is a choice, drift in existing code is a regression.
"""

from __future__ import annotations

from typing import Any

from cgir.ir.component_spec import ComponentSpec

CONTRACT_FIELDS = ("kind", "purity", "effects", "signature", "outputs")


def compute_diff(old_specs: list[ComponentSpec], new_specs: list[ComponentSpec]) -> dict[str, Any]:
    old = {s.id: s for s in old_specs}
    new = {s.id: s for s in new_specs}

    changed: list[dict[str, Any]] = []
    for spec_id in sorted(old.keys() & new.keys()):
        fields = _field_changes(old[spec_id], new[spec_id])
        if fields:
            changed.append({"id": spec_id, "fields": fields})

    return {
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
        "changed": changed,
    }


def _field_changes(old: ComponentSpec, new: ComponentSpec) -> dict[str, dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}
    for name in CONTRACT_FIELDS:
        old_value = getattr(old, name)
        new_value = getattr(new, name)
        if name == "kind":
            old_value, new_value = old_value.value, new_value.value
        if old_value != new_value:
            fields[name] = {"old": old_value, "new": new_value}
    return fields


def violations(diff: dict[str, Any], rules: list[str]) -> list[str]:
    """Evaluate fail rules against a diff; each hit is a human-readable line."""
    found: list[str] = []
    for change in diff["changed"]:
        fields = change["fields"]
        spec_id = change["id"]
        for rule in rules:
            if rule.startswith("effect-gain"):
                _, _, wanted = rule.partition(":")
                gained = _gained_effects(fields)
                if wanted:
                    gained = [t for t in gained if t == wanted]
                if gained:
                    found.append(f"{spec_id}: gained effect(s) {', '.join(gained)}")
            elif rule == "purity-drop":
                purity = fields.get("purity")
                if purity and _as_float(purity["new"]) < _as_float(purity["old"]):
                    found.append(f"{spec_id}: purity dropped {purity['old']} -> {purity['new']}")
            elif rule == "kind-change":
                kind = fields.get("kind")
                if kind:
                    found.append(f"{spec_id}: kind changed {kind['old']} -> {kind['new']}")
    return found


def _gained_effects(fields: dict[str, dict[str, Any]]) -> list[str]:
    effects = fields.get("effects")
    if not effects:
        return []
    return sorted(set(effects["new"]) - set(effects["old"]))


def _as_float(value: Any) -> float:
    return float(value) if value is not None else 0.0


def render_diff(diff: dict[str, Any]) -> str:
    if not (diff["added"] or diff["removed"] or diff["changed"]):
        return "no changes\n"
    lines: list[str] = []
    if diff["added"]:
        lines.append(f"added ({len(diff['added'])}):")
        lines.extend(f"  + {spec_id}" for spec_id in diff["added"])
    if diff["removed"]:
        lines.append(f"removed ({len(diff['removed'])}):")
        lines.extend(f"  - {spec_id}" for spec_id in diff["removed"])
    if diff["changed"]:
        lines.append(f"changed ({len(diff['changed'])}):")
        for change in diff["changed"]:
            lines.append(f"  ~ {change['id']}")
            for name, values in change["fields"].items():
                lines.append(f"      {name}: {values['old']} -> {values['new']}")
    return "\n".join(lines) + "\n"
