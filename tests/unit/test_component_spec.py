import json

import pytest
from jsonschema import ValidationError

from cgir.ir.component_spec import ComponentKind, ComponentSpec


def test_validates_round_trip() -> None:
    spec = ComponentSpec(
        id="pricing.add_tax",
        kind=ComponentKind.pure_function,
        inputs=["price", "rate"],
        outputs=["float"],
        effects=[],
        calls=[],
        trace=["pricing.py:1"],
        language="python",
        signature="add_tax(price: float, rate: float) -> float",
        purity=1.0,
    )
    spec.validate()
    data = json.loads(spec.to_json())
    assert ComponentSpec.from_dict(data).to_dict() == spec.to_dict()


def test_constructs_field_round_trips() -> None:
    """Sprint 13 schema addition: which in-repo types a component constructs."""
    spec = ComponentSpec(
        id="routes.create_chapter",
        kind=ComponentKind.effect_adapter,
        inputs=["db", "payload"],
        outputs=["Chapter"],
        effects=["db"],
        calls=[],
        constructs=["models.chapter.Chapter"],
        trace=["routes.py:10"],
        language="python",
        purity=0.0,
    )
    spec.validate()
    data = json.loads(spec.to_json())
    restored = ComponentSpec.from_dict(data)
    assert restored.constructs == ["models.chapter.Chapter"]


def test_doc_and_raises_round_trip() -> None:
    """Sprint 23 schema additions: behavior contract (docstring + raises)."""
    spec = ComponentSpec(
        id="m.f",
        kind=ComponentKind.pure_function,
        trace=["m.py:1"],
        doc="Return x plus one.",
        raises=["ValueError"],
    )
    spec.validate()
    restored = ComponentSpec.from_dict(json.loads(spec.to_json()))
    assert restored.doc == "Return x plus one."
    assert restored.raises == ["ValueError"]


def test_entrypoint_field_round_trips() -> None:
    """Sprint 17 schema addition: how the outside world reaches a component."""
    spec = ComponentSpec(
        id="routes.get_novel",
        kind=ComponentKind.orchestrator,
        inputs=["novel_id"],
        outputs=["Novel"],
        effects=["raise"],
        calls=[],
        trace=["routes.py:10"],
        language="python",
        purity=0.7,
        entrypoint="HTTP GET /novels/{novel_id}",
    )
    spec.validate()
    data = json.loads(spec.to_json())
    assert ComponentSpec.from_dict(data).entrypoint == "HTTP GET /novels/{novel_id}"


def test_rejects_bad_kind() -> None:
    with pytest.raises(ValidationError):
        bad = {
            "id": "x",
            "kind": "not_a_kind",
            "inputs": [],
            "outputs": [],
            "effects": [],
            "calls": [],
            "trace": [],
        }
        from cgir.ir.component_spec import _validator

        _validator().validate(bad)
