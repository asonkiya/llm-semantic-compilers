"""RED-phase tests for the context packer (Sprint 18 — `cgir pack`).

Contract:

* ``build_pack(specs, target_id, source=None, budget=4000) -> dict`` —
  pure over specs (+ optional target source text), JSON-able. Sections in
  priority order: target (spec + source), callee interfaces, caller
  usages, constructed types. ``budget`` is an approximate token budget
  (chars / 4); lower-priority sections are dropped to fit and recorded
  under ``omitted``.
* ``render_pack(pack) -> str`` — markdown for direct prompt use.
* Unknown target raises ``KeyError``.
"""

from __future__ import annotations

import pytest

from cgir.ir.component_spec import ComponentKind, ComponentSpec
from cgir.report.pack import build_pack, render_pack


def _spec(
    spec_id: str,
    kind: ComponentKind = ComponentKind.pure_function,
    calls: list[str] | None = None,
    effects: list[str] | None = None,
    signature: str | None = None,
    outputs: list[str] | None = None,
    entrypoint: str | None = None,
) -> ComponentSpec:
    return ComponentSpec(
        id=spec_id,
        kind=kind,
        inputs=[],
        outputs=outputs or [],
        effects=effects or [],
        calls=calls or [],
        trace=[f"{spec_id.split('.')[0]}.py:1"],
        language="python",
        signature=signature or f"{spec_id.split('.')[-1]}()",
        entrypoint=entrypoint,
        purity=1.0,
    )


def _world() -> list[ComponentSpec]:
    return [
        _spec(
            "repos.get_novel",
            kind=ComponentKind.effect_adapter,
            effects=["db"],
            signature="get_novel(db: Session, novel_id: int)",
            outputs=["Novel | None"],
        ),
        _spec(
            "services.translate",
            calls=["repos.get_novel"],
            signature="translate(db: Session, novel_id: int)",
        ),
        _spec(
            "routes.translate_one",
            calls=["services.translate"],
            signature="translate_one(db: Session, novel_id: int)",
            entrypoint="HTTP POST /translate",
        ),
    ]


def test_target_section_present() -> None:
    pack = build_pack(_world(), "services.translate")
    assert pack["target"]["id"] == "services.translate"
    assert pack["target"]["signature"] == "translate(db: Session, novel_id: int)"


def test_callee_interfaces_included() -> None:
    pack = build_pack(_world(), "services.translate")
    [callee] = pack["callees"]
    assert callee["id"] == "repos.get_novel"
    assert callee["outputs"] == ["Novel | None"]
    assert callee["effects"] == ["db"]


def test_caller_usages_included_with_entrypoint() -> None:
    pack = build_pack(_world(), "services.translate")
    [caller] = pack["callers"]
    assert caller["id"] == "routes.translate_one"
    assert caller["entrypoint"] == "HTTP POST /translate"


def test_source_text_embedded_when_given() -> None:
    src = "def translate(db, novel_id):\n    return get_novel(db, novel_id)\n"
    pack = build_pack(_world(), "services.translate", source=src)
    assert pack["target"]["source"] == src


def test_budget_drops_low_priority_sections() -> None:
    """A tiny budget keeps the target but drops callers and records it."""
    pack = build_pack(_world(), "services.translate", budget=40)
    assert pack["target"]["id"] == "services.translate"
    assert pack["callers"] == []
    assert "callers" in pack["omitted"]


def test_unknown_target_raises() -> None:
    with pytest.raises(KeyError):
        build_pack(_world(), "nope.nothing")


def test_render_is_markdown_with_sections() -> None:
    text = render_pack(build_pack(_world(), "services.translate"))
    assert text.startswith("# services.translate")
    assert "## Callees (interfaces)" in text
    assert "## Callers" in text
    assert "repos.get_novel" in text
