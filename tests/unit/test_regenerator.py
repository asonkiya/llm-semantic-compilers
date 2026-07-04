"""RED-phase tests for regeneration (milestone: P1-regenerate).

Contract:

* ``regenerate(spec, target_language, generator=None) -> RegenerationResult``.
* ``generator`` is an injectable ``Callable[[str], str]`` — the seam for
  the LLM call. Tests inject a fake; no network, per the local-first rule
  (only the regeneration step may touch an LLM, gated on ComponentSpec).
* With a generator: ``result.code`` is the generator's output over the
  prompt-pack and ``result.live`` is True.
* Without one: dry run — ``result.live`` is False and ``result.code``
  explains how to run live. No ``STUB``/``milestone`` markers remain.
* ``anthropic_generator()`` raises a clear error when the optional
  ``anthropic`` package is missing (install hint), without importing it
  at module import time.
"""

from __future__ import annotations

import pytest

from cgir.ir.component_spec import ComponentKind, ComponentSpec
from cgir.regenerate.regenerator import anthropic_generator, regenerate


def _spec() -> ComponentSpec:
    return ComponentSpec(
        id="pricing.add_tax",
        kind=ComponentKind.pure_function,
        inputs=["price", "rate"],
        outputs=[],
        effects=[],
        calls=[],
        trace=["pricing.py:1"],
        language="python",
        signature="def add_tax(price, rate)",
        purity=1.0,
    )


def test_injected_generator_drives_live_result() -> None:
    result = regenerate(_spec(), "typescript", generator=lambda prompt: "export const x = 1;")
    assert result.code == "export const x = 1;"
    assert result.live is True


def test_generator_receives_the_prompt_pack() -> None:
    seen: list[str] = []

    def capture(prompt: str) -> str:
        seen.append(prompt)
        return "ok"

    regenerate(_spec(), "typescript", generator=capture)
    assert len(seen) == 1
    assert "pricing.add_tax" in seen[0]
    assert "typescript" in seen[0]


def test_dry_run_without_generator() -> None:
    result = regenerate(_spec(), "typescript")
    assert result.live is False
    assert "dry run" in result.code.lower()
    assert "STUB" not in result.code
    assert "milestone" not in result.code


def test_anthropic_generator_missing_dependency_message() -> None:
    try:
        import anthropic  # noqa: F401

        pytest.skip("anthropic installed; missing-dependency path not reachable")
    except ImportError:
        pass
    with pytest.raises(RuntimeError, match=r"cgir\[llm\]"):
        anthropic_generator()
