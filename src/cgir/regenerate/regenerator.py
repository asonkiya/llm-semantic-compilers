"""LLM-driven regeneration — milestone: P1-regenerate.

Wired far enough to print the prompt-pack and a placeholder. The real
implementation will call into the Anthropic SDK and round-trip the
generated code through compile/test verification before tagging the
``REGENERATED_AS`` edge.
"""

from __future__ import annotations

from dataclasses import dataclass

from cgir.ir.component_spec import ComponentSpec
from cgir.regenerate.prompt_pack import build_prompt


@dataclass(slots=True)
class RegenerationResult:
    spec_id: str
    target_language: str
    prompt: str
    code: str


def regenerate(spec: ComponentSpec, target_language: str) -> RegenerationResult:
    prompt = build_prompt(spec, target_language)
    # STUB: P1-regenerate — no LLM call yet.
    placeholder = (
        f"// STUB regeneration of {spec.id} in {target_language}\n// milestone: P1-regenerate\n"
    )
    return RegenerationResult(
        spec_id=spec.id,
        target_language=target_language,
        prompt=prompt,
        code=placeholder,
    )
