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
