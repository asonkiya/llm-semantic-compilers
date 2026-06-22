"""Render the prompt-pack template from Code-IR.md §Analysis/workflow."""

from __future__ import annotations

from cgir.ir.component_spec import ComponentSpec

PROMPT_TEMPLATE = (
    "Given ComponentSpec + dependent interfaces + tests, recreate this component in "
    "{target_language}. Preserve contracts, effects, and trace IDs. Do not invent "
    "hidden dependencies.\n\n"
    "ComponentSpec:\n{spec_json}\n"
)


def build_prompt(spec: ComponentSpec, target_language: str) -> str:
    return PROMPT_TEMPLATE.format(target_language=target_language, spec_json=spec.to_json())
