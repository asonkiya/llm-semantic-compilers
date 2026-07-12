"""Index diff — architecture drift between two scans (Sprint 16).

``compute_diff`` is pure over two spec lists (same pattern as
:mod:`cgir.report.stats`): which components appeared, disappeared, or
changed on the *contract* fields — kind, purity, effects, signature,
outputs. ``violations`` evaluates CI fail rules against a diff, so
"this PR made a pure function effectful" — or silently dropped a network
call — can fail a build:

    cgir diff old-index new-index --fail-on effect-gain:net --fail-on effect-loss:net

Rules only fire for components present in *both* scans — new effectful
code is a choice, drift in existing code is a regression.
"""

from __future__ import annotations

from typing import Any

from cgir.ir.component_spec import ComponentSpec

CONTRACT_FIELDS = ("kind", "purity", "effects", "signature", "outputs")


def compute_diff(
    old_specs: list[ComponentSpec],
    new_specs: list[ComponentSpec],
    old_types: dict[str, dict[str, str]] | None = None,
    new_types: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    old = {s.id: s for s in old_specs}
    new = {s.id: s for s in new_specs}

    changed: list[dict[str, Any]] = []
    for spec_id in sorted(old.keys() & new.keys()):
        fields = _field_changes(old[spec_id], new[spec_id])
        if fields:
            changed.append(
                {
                    "id": spec_id,
                    "fields": fields,
                    # confidence metadata (not a contract field): lets gate
                    # rules ignore lexically-inferred tags by default.
                    "lexical": {
                        "old": list(old[spec_id].lexical_effects),
                        "new": list(new[spec_id].lexical_effects),
                    },
                }
            )

    return {
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
        "changed": changed,
        "entrypoints": _entrypoint_surface(old, new),
        "types": _type_shape_changes(old_types or {}, new_types or {}, new_specs),
    }


def _type_shape_changes(
    old_types: dict[str, dict[str, str]],
    new_types: dict[str, dict[str, str]],
    new_specs: list[ComponentSpec],
) -> dict[str, Any]:
    """Field-level drift per type: "the rewrite dropped a field" made visible.

    ``referenced_by`` names the components whose contract (outputs/params via
    :func:`referenced_type_names`) mentions the drifted type — the blast
    surface a shape change actually has.
    """
    changed: list[dict[str, Any]] = []
    for name in sorted(old_types.keys() & new_types.keys()):
        before, after = old_types[name], new_types[name]
        if before == after:
            continue
        entry: dict[str, Any] = {
            "name": name,
            "added": sorted(after.keys() - before.keys()),
            "removed": sorted(before.keys() - after.keys()),
            "changed": {
                f: {"old": before[f], "new": after[f]}
                for f in sorted(before.keys() & after.keys())
                if before[f] != after[f]
            },
        }
        short = name.rsplit(".", 1)[-1]
        entry["referenced_by"] = sorted(
            s.id for s in new_specs if short in _referenced_type_names(s)
        )
        changed.append(entry)
    return {"changed": changed}


def _referenced_type_names(spec: ComponentSpec) -> set[str]:
    from cgir.report.pack import referenced_type_names

    return referenced_type_names(spec)


def _entrypoint_surface(
    old: dict[str, ComponentSpec], new: dict[str, ComponentSpec]
) -> dict[str, list[dict[str, Any]]]:
    """The externally-reachable surface that appeared, vanished, or moved."""
    added = [
        {"id": sid, "entrypoint": new[sid].entrypoint}
        for sid in sorted(new.keys() - old.keys())
        if new[sid].entrypoint
    ]
    removed = [
        {"id": sid, "entrypoint": old[sid].entrypoint}
        for sid in sorted(old.keys() - new.keys())
        if old[sid].entrypoint
    ]
    changed = [
        {"id": sid, "old": old[sid].entrypoint, "new": new[sid].entrypoint}
        for sid in sorted(old.keys() & new.keys())
        if old[sid].entrypoint != new[sid].entrypoint
    ]
    return {"added": added, "removed": removed, "changed": changed}


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
        lexical = change.get("lexical", {"old": [], "new": []})
        for rule in rules:
            if rule.startswith("effect-gain"):
                rule, include_lexical = _strip_any(rule)
                _, _, wanted = rule.partition(":")
                gained = _gained_effects(fields)
                if not include_lexical:
                    # a tag backed only by lexical evidence (suffix/receiver
                    # heuristics) is low-confidence — report, don't fail.
                    gained = [t for t in gained if t not in lexical["new"]]
                if wanted:
                    gained = [t for t in gained if t == wanted]
                if gained:
                    found.append(f"{spec_id}: gained effect(s) {', '.join(gained)}")
            elif rule.startswith("effect-loss"):
                rule, include_lexical = _strip_any(rule)
                _, _, wanted = rule.partition(":")
                lost = _lost_effects(fields)
                if not include_lexical:
                    lost = [t for t in lost if t not in lexical["old"]]
                if wanted:
                    lost = [t for t in lost if t == wanted]
                # Indirection, not removal: if the component *simultaneously*
                # gained calls_effectful, the effect most likely moved behind a
                # call and is still transitively reachable (the one false-alarm
                # class in the real-history noise replay — see gate-noise.md).
                # Trade-off: a true removal paired with a new unrelated
                # effectful call is masked; the loss stays visible in the diff
                # report, it just doesn't fail the build.
                if lost and "calls_effectful" in _gained_effects(fields):
                    lost = []
                if lost:
                    found.append(f"{spec_id}: lost effect(s) {', '.join(lost)}")
            elif rule == "purity-drop":
                purity = fields.get("purity")
                if purity and _as_float(purity["new"]) < _as_float(purity["old"]):
                    found.append(f"{spec_id}: purity dropped {purity['old']} -> {purity['new']}")
            elif rule == "kind-change":
                kind = fields.get("kind")
                if kind:
                    found.append(f"{spec_id}: kind changed {kind['old']} -> {kind['new']}")
    surface = diff.get("entrypoints", {})
    for rule in rules:
        if rule == "entrypoint-added":
            for e in surface.get("added", []):
                found.append(f"{e['id']}: new entrypoint {e['entrypoint']}")
        elif rule == "entrypoint-change":
            for e in surface.get("changed", []):
                found.append(f"{e['id']}: entrypoint changed {e['old']} -> {e['new']}")
        elif rule == "shape-change":
            # Only referenced types fire: an internal shape nobody's contract
            # names is a private refactor, not drift.
            for t in diff.get("types", {}).get("changed", []):
                if not t["referenced_by"]:
                    continue
                deltas = []
                if t["removed"]:
                    deltas.append(f"removed {', '.join(t['removed'])}")
                if t["added"]:
                    deltas.append(f"added {', '.join(t['added'])}")
                if t["changed"]:
                    deltas.append(f"retyped {', '.join(t['changed'])}")
                found.append(
                    f"{t['name']}: shape changed ({'; '.join(deltas)}) — "
                    f"referenced by {', '.join(t['referenced_by'])}"
                )
    return found


def _strip_any(rule: str) -> tuple[str, bool]:
    """``effect-gain:net:any`` → (``effect-gain:net``, True): opt into
    firing on lexically-inferred tags too."""
    if rule.endswith(":any"):
        return rule[: -len(":any")], True
    return rule, False


def _gained_effects(fields: dict[str, dict[str, Any]]) -> list[str]:
    effects = fields.get("effects")
    if not effects:
        return []
    return sorted(set(effects["new"]) - set(effects["old"]))


def _lost_effects(fields: dict[str, dict[str, Any]]) -> list[str]:
    effects = fields.get("effects")
    if not effects:
        return []
    return sorted(set(effects["old"]) - set(effects["new"]))


def _as_float(value: Any) -> float:
    return float(value) if value is not None else 0.0


def render_diff(diff: dict[str, Any]) -> str:
    surface = diff.get("entrypoints", {"added": [], "removed": [], "changed": []})
    has_surface = surface["added"] or surface["removed"] or surface["changed"]
    type_changes = diff.get("types", {}).get("changed", [])
    if not (diff["added"] or diff["removed"] or diff["changed"] or has_surface or type_changes):
        return "no changes\n"
    lines: list[str] = []
    if type_changes:
        lines.append(f"type shapes changed ({len(type_changes)}):")
        for t in type_changes:
            deltas = [f"-{f}" for f in t["removed"]] + [f"+{f}" for f in t["added"]]
            deltas += [f"~{f}" for f in t["changed"]]
            refs = (
                f"  (referenced by {', '.join(t['referenced_by'])})" if t["referenced_by"] else ""
            )
            lines.append(f"  ~ {t['name']}: {' '.join(deltas)}{refs}")
    if has_surface:
        lines.append("entrypoint surface:")
        lines.extend(f"  + {e['entrypoint']}  ({e['id']})" for e in surface["added"])
        lines.extend(f"  - {e['entrypoint']}  ({e['id']})" for e in surface["removed"])
        lines.extend(f"  ~ {e['old']} -> {e['new']}  ({e['id']})" for e in surface["changed"])
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


def render_diff_markdown(
    diff: dict[str, Any],
    violations: list[str] | None = None,
    warning: str | None = None,
) -> str:
    """A PR-comment-ready markdown report of an index diff."""
    surface = diff.get("entrypoints", {"added": [], "removed": [], "changed": []})
    has_surface = bool(surface["added"] or surface["removed"] or surface["changed"])
    has_any = bool(diff["added"] or diff["removed"] or diff["changed"] or has_surface)

    out: list[str] = ["## 🔍 CGIR contract diff", ""]
    if warning:
        out += [f"> ⚠️ {warning}", ""]
    if not has_any:
        out.append("✅ No contract changes.")
        return "\n".join(out) + "\n"

    if violations:
        out.append(f"> ❌ **{len(violations)} drift violation(s)** — gate failed:")
        out += [f"> - {line}" for line in violations]
        out.append("")

    if has_surface:
        out += ["### Entrypoint surface", ""]
        out += [f"- 🟢 `{e['entrypoint']}` — new (`{e['id']}`)" for e in surface["added"]]
        out += [f"- 🔴 `{e['entrypoint']}` — removed (`{e['id']}`)" for e in surface["removed"]]
        out += [f"- 🟡 `{e['old']}` → `{e['new']}` (`{e['id']}`)" for e in surface["changed"]]
        out.append("")

    if diff["changed"]:
        out += [f"### Contract drift ({len(diff['changed'])})", ""]
        for change in diff["changed"]:
            out.append(f"<details><summary><code>{change['id']}</code></summary>")
            out += ["", "| field | before | after |", "|---|---|---|"]
            for name, values in change["fields"].items():
                old_v = _md_cell(values["old"])
                new_v = _md_cell(values["new"])
                out.append(f"| {name} | {old_v} | {new_v} |")
            out += ["", "</details>", ""]

    counts = []
    if diff["added"]:
        counts.append(f"**{len(diff['added'])}** added")
    if diff["removed"]:
        counts.append(f"**{len(diff['removed'])}** removed")
    if counts:
        out.append(" · ".join(counts))

    return "\n".join(out).rstrip() + "\n"


def _md_cell(value: Any) -> str:
    if value is None or value == [] or value == "":
        return "—"
    if isinstance(value, list):
        return ", ".join(f"`{v}`" for v in value)
    return f"`{value}`"
