"""LLM-driven regeneration.

The generation seam is an injectable ``Callable[[str], str]`` so the
pipeline stays testable offline — only :func:`anthropic_generator` touches
the network, and only when explicitly requested (``cgir regenerate
--live``). Per the spec, the LLM sees the prompt-pack built from the
``ComponentSpec``, never raw source.

Round-tripping generated code through compile/test verification before
tagging ``REGENERATED_AS`` edges is future work — the result carries
``live`` so callers can tell a real generation from a dry run.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from cgir.ir.component_spec import ComponentSpec
from cgir.regenerate.prompt_pack import build_prompt

DEFAULT_MODEL = "claude-sonnet-4-6"

_SYSTEM_PROMPT = (
    "You are a code regeneration engine. You receive a ComponentSpec (a "
    "language-agnostic contract for one component: inputs, effects, calls, "
    "purity, trace IDs) and produce an implementation in the requested "
    "target language. Respond with code only — no prose, no fences."
)

_DRY_RUN_NOTE = (
    "// dry run: no generator supplied.\n"
    "// Pass --live (requires `pip install cgir[llm]` and ANTHROPIC_API_KEY)\n"
    "// or inject a generator callable to produce code.\n"
)


@dataclass(slots=True)
class RegenerationResult:
    spec_id: str
    target_language: str
    prompt: str
    code: str
    live: bool = False


def regenerate(
    spec: ComponentSpec,
    target_language: str,
    generator: Callable[[str], str] | None = None,
) -> RegenerationResult:
    """Build the prompt-pack for ``spec`` and (optionally) generate code.

    Without a ``generator`` this is a dry run: the prompt is returned for
    inspection and ``code`` explains how to go live.
    """
    prompt = build_prompt(spec, target_language)
    if generator is None:
        return RegenerationResult(
            spec_id=spec.id,
            target_language=target_language,
            prompt=prompt,
            code=_DRY_RUN_NOTE,
            live=False,
        )
    return RegenerationResult(
        spec_id=spec.id,
        target_language=target_language,
        prompt=prompt,
        code=generator(prompt),
        live=True,
    )


def anthropic_generator(model: str | None = None) -> Callable[[str], str]:
    """A generator backed by the Anthropic SDK (``pip install cgir[llm]``).

    The system prompt is marked for prompt caching from day one (see
    ``docs/roadmap.md``). Model defaults to :data:`DEFAULT_MODEL`; override
    with the ``CGIR_MODEL`` environment variable or the ``model`` argument.
    """
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "Install cgir[llm] to use live regeneration (adds the anthropic package)"
        ) from exc

    client = anthropic.Anthropic()
    resolved_model = model or os.environ.get("CGIR_MODEL", DEFAULT_MODEL)

    def generate(prompt: str) -> str:
        message = client.messages.create(
            model=resolved_model,
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in message.content if block.type == "text")

    return generate
