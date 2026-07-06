"""Write the repo graph + ComponentSpec index to disk."""

from __future__ import annotations

import json
from pathlib import Path

from cgir.ir.component_spec import ComponentSpec
from cgir.ir.graph import RepoGraph
from cgir.manifest import write_manifest


def read_specs(index_dir: Path) -> list[ComponentSpec]:
    """Load every ComponentSpec from an existing index directory."""
    components_dir = index_dir / "components"
    return [
        ComponentSpec.from_dict(json.loads(p.read_text()))
        for p in sorted(components_dir.glob("*.json"))
    ]


def write_index(out_dir: Path, graph: RepoGraph, specs: list[ComponentSpec]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "repo_graph.json").write_text(
        json.dumps(graph.to_jsonable(), indent=2, sort_keys=True)
    )

    components_dir = out_dir / "components"
    components_dir.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        spec.validate()
        (components_dir / f"{spec.id}.json").write_text(spec.to_json())

    index = [{"id": spec.id, "kind": spec.kind.value, "trace": spec.trace} for spec in specs]
    (out_dir / "components_index.json").write_text(json.dumps(index, indent=2, sort_keys=True))

    write_manifest(out_dir, component_count=len(specs))
