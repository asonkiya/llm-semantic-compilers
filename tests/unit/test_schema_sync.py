"""The published JSON schema and the runtime source of truth must agree.

`schemas/component_spec.schema.json` is the contract shipped in the wheel;
`cgir.ir.component_spec.COMPONENT_SPEC_SCHEMA` is what the code validates
against. CLAUDE.md requires changing both together — this test is the guard.
"""

import json
from pathlib import Path

from cgir.ir.component_spec import COMPONENT_SPEC_SCHEMA

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "component_spec.schema.json"


def test_published_schema_matches_runtime() -> None:
    disk = json.loads(SCHEMA_PATH.read_text())
    assert disk == COMPONENT_SPEC_SCHEMA
