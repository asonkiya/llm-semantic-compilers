"""Context packer — the minimal bundle for working on one component.

This is the product's core loop (Code-IR.md: rewrite/audit "without
holding the whole repo in context"): given a target component, assemble
what an LLM needs and nothing else, in priority order:

1. the target — spec fields + source text when available
2. its callees, as *interfaces only* (signature, kind, effects, returns)
3. its callers — how it's used, with entrypoints ("how the outside
   world reaches this")
4. the types it constructs

``budget`` is an approximate token budget (chars / 4). Lower-priority
sections are dropped whole to fit, and every drop is recorded under
``omitted`` — the bundle never silently lies about completeness.
"""

from __future__ import annotations

import re
from typing import Any

from cgir.ir.component_spec import ComponentSpec

DEFAULT_BUDGET = 4000

# Dropped in this order to fit budget: types, tests & the target contract
# are the last to go (they're what makes a context-free rewrite possible).
_SECTION_PRIORITY = ("constructs", "callers", "callees", "tests", "types")

_BUILTIN_TYPES = frozenset(
    {
        "int",
        "float",
        "str",
        "bool",
        "bytes",
        "None",
        "Any",
        "list",
        "dict",
        "set",
        "tuple",
        "frozenset",
        "object",
        "Optional",
        "Union",
        "Iterable",
        "Iterator",
        "Sequence",
        "Mapping",
        "Callable",
        "Type",
        "Awaitable",
        "Coroutine",
        "AsyncIterator",
    }
)
_TYPE_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def referenced_type_names(spec: ComponentSpec) -> set[str]:
    """Non-builtin type identifiers the component's contract names.

    Pulled from the return type, constructed classes, and parameter
    annotations — the types an implementer must know the shape of.
    """
    names: set[str] = set()
    for output in spec.outputs:
        names |= _type_tokens(output)
    for construct in spec.constructs:
        names.add(construct.rsplit(".", 1)[-1])
    for _, annotation in _param_items(spec.signature):
        if annotation:
            names |= _type_tokens(annotation)
    return {n for n in names if n not in _BUILTIN_TYPES}


def _type_tokens(annotation: str) -> set[str]:
    return set(_TYPE_TOKEN.findall(annotation))


def _param_items(signature: str | None) -> list[tuple[str, str | None]]:
    if not signature or "(" not in signature or ")" not in signature:
        return []
    inner = signature[signature.index("(") + 1 : signature.rindex(")")]
    items: list[tuple[str, str | None]] = []
    depth, current = 0, ""
    for ch in inner + ",":
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            name, _, annotation = current.partition(":")
            annotation = annotation.split("=", 1)[0].strip()
            items.append((name.strip().lstrip("*"), annotation or None))
            current = ""
        else:
            current += ch
    return items


def build_pack(
    specs: list[ComponentSpec],
    target_id: str,
    source: str | None = None,
    budget: int = DEFAULT_BUDGET,
    types: dict[str, str] | None = None,
    tests: dict[str, str] | None = None,
) -> dict[str, Any]:
    by_id = {s.id: s for s in specs}
    target = by_id[target_id]  # KeyError for unknown targets, by design

    callees = [_interface(by_id[callee]) for callee in target.calls if callee in by_id]
    callers = [_interface(s) for s in specs if target_id in s.calls]
    type_defs = [{"name": name, "source": src} for name, src in sorted((types or {}).items())]
    test_defs = [{"id": tid, "source": src} for tid, src in sorted((tests or {}).items())]
    pack: dict[str, Any] = {
        "target": {
            "id": target.id,
            "kind": target.kind.value,
            "signature": target.signature,
            "inputs": target.inputs,
            "outputs": target.outputs,
            "effects": target.effects,
            "purity": target.purity,
            "entrypoint": target.entrypoint,
            "doc": target.doc,
            "raises": target.raises,
            "trace": target.trace,
            "source": source,
        },
        "types": type_defs,
        "tests": test_defs,
        "callees": callees,
        "callers": callers,
        "constructs": list(target.constructs),
        "omitted": [],
    }

    for section in _SECTION_PRIORITY:
        if _estimate_tokens(pack) <= budget:
            break
        if pack[section]:
            pack[section] = []
            pack["omitted"].append(section)
    return pack


def _interface(spec: ComponentSpec) -> dict[str, Any]:
    return {
        "id": spec.id,
        "kind": spec.kind.value,
        "signature": spec.signature,
        "outputs": spec.outputs,
        "effects": spec.effects,
        "entrypoint": spec.entrypoint,
    }


def _estimate_tokens(pack: dict[str, Any]) -> int:
    return len(str(pack)) // 4


def render_pack(pack: dict[str, Any]) -> str:
    target = pack["target"]
    lines: list[str] = [f"# {target['id']}", ""]
    lines.append(
        f"`{target['signature']}` — {target['kind']}"
        + (f", purity {target['purity']}" if target["purity"] is not None else "")
    )
    if target["entrypoint"]:
        lines.append(f"Entrypoint: **{target['entrypoint']}**")
    if target["effects"]:
        lines.append(f"Effects: {', '.join(target['effects'])}")
    if target["outputs"]:
        lines.append(f"Returns: {', '.join(target['outputs'])}")
    if target.get("raises"):
        lines.append(f"Raises: {', '.join(target['raises'])}")
    if target["trace"]:
        lines.append(f"Source location: {target['trace'][0]}")
    if target.get("doc"):
        lines.append("")
        lines.append(f"> {target['doc'].strip().splitlines()[0]}")
    if target["source"]:
        lines.append("")
        lines.append("```python")
        lines.append(target["source"].rstrip("\n"))
        lines.append("```")

    if pack.get("types"):
        lines.append("")
        lines.append("## Types")
        lines.append("Shapes the target's contract references — match these exactly.")
        for tdef in pack["types"]:
            lines.append("")
            lines.append("```python")
            lines.append(tdef["source"].rstrip("\n"))
            lines.append("```")

    if pack.get("tests"):
        lines.append("")
        lines.append("## Tests (behavior contract)")
        lines.append("The implementation must satisfy these.")
        for tdef in pack["tests"]:
            lines.append("")
            lines.append(f"```python  # {tdef['id']}")
            lines.append(tdef["source"].rstrip("\n"))
            lines.append("```")

    if pack["callees"]:
        lines.append("")
        lines.append("## Callees (interfaces)")
        lines.append("Do not modify these; call them as specified.")
        lines.append("")
        for callee in pack["callees"]:
            lines.append(_interface_line(callee))
    if pack["callers"]:
        lines.append("")
        lines.append("## Callers")
        lines.append("These depend on the target's current contract.")
        lines.append("")
        for caller in pack["callers"]:
            lines.append(_interface_line(caller))
    if pack["constructs"]:
        lines.append("")
        lines.append("## Constructs")
        lines.extend(f"- {type_name}" for type_name in pack["constructs"])
    if pack["omitted"]:
        lines.append("")
        lines.append(f"_Omitted for budget: {', '.join(pack['omitted'])}_")
    return "\n".join(lines) + "\n"


def _interface_line(entry: dict[str, Any]) -> str:
    parts = [f"- `{entry['signature']}`", f"({entry['id']}, {entry['kind']}"]
    if entry["effects"]:
        parts[-1] += f", effects: {','.join(entry['effects'])}"
    parts[-1] += ")"
    if entry["outputs"]:
        parts.append(f"-> {entry['outputs'][0]}")
    if entry["entrypoint"]:
        parts.append(f"[{entry['entrypoint']}]")
    return " ".join(parts)
