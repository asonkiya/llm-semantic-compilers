"""ComponentSpec — the agent-facing contract (Code-IR.md §Data model)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator

COMPONENT_SPEC_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://cgir.dev/schemas/component_spec.schema.json",
    "title": "ComponentSpec",
    "type": "object",
    "required": ["id", "kind", "inputs", "outputs", "effects", "calls", "trace"],
    "properties": {
        "id": {"type": "string"},
        "kind": {
            "enum": [
                "pure_function",
                "state_transformer",
                "effect_adapter",
                "orchestrator",
                "unknown",
            ]
        },
        "language": {"type": "string"},
        "signature": {"type": "string"},
        "entrypoint": {"type": "string"},
        "doc": {"type": "string"},
        "raises": {"type": "array", "items": {"type": "string"}},
        "covered_by": {"type": "array", "items": {"type": "string"}},
        "inputs": {"type": "array", "items": {"type": "string"}},
        "outputs": {"type": "array", "items": {"type": "string"}},
        "effects": {"type": "array", "items": {"type": "string"}},
        "calls": {"type": "array", "items": {"type": "string"}},
        "constructs": {"type": "array", "items": {"type": "string"}},
        "reads": {"type": "array", "items": {"type": "string"}},
        "writes": {"type": "array", "items": {"type": "string"}},
        "purity": {"type": "number", "minimum": 0, "maximum": 1},
        "pins": {"type": "array", "items": {"type": "string"}},
        "algorithm": {"type": "array", "items": {"type": "string"}},
        "trace": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


class ComponentKind(StrEnum):
    pure_function = "pure_function"
    state_transformer = "state_transformer"
    effect_adapter = "effect_adapter"
    orchestrator = "orchestrator"
    unknown = "unknown"


@dataclass(slots=True)
class ComponentSpec:
    id: str
    kind: ComponentKind
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    constructs: list[str] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    language: str | None = None
    signature: str | None = None
    entrypoint: str | None = None
    doc: str | None = None
    raises: list[str] = field(default_factory=list)
    covered_by: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    purity: float | None = None
    pins: list[str] = field(default_factory=list)
    algorithm: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return {k: v for k, v in data.items() if v is not None}

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ComponentSpec:
        payload = dict(data)
        payload["kind"] = ComponentKind(payload["kind"])
        return cls(**payload)

    def validate(self) -> None:
        _validator().validate(self.to_dict())


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    return Draft202012Validator(COMPONENT_SPEC_SCHEMA)
