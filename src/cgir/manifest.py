"""Index manifest — provenance + compatibility for a written index.

Every index carries a ``manifest.json`` recording which ``cgir`` version
and ComponentSpec schema version produced it. ``cgir diff`` (and any tool
comparing two indexes, e.g. the CI Action) reads both manifests so a
base/head comparison produced by *different* cgir versions is a warning,
not a silent wrong answer.

``SCHEMA_VERSION`` bumps whenever the ComponentSpec contract changes shape
(a field added/removed/repurposed). It is independent of ``cgir.__version__``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cgir import __version__

SCHEMA_VERSION = "1.0"
MANIFEST_NAME = "manifest.json"


@dataclass(slots=True)
class Manifest:
    cgir_version: str
    schema_version: str
    component_count: int
    created_at: str

    @classmethod
    def create(cls, component_count: int) -> Manifest:
        return cls(
            cgir_version=__version__,
            schema_version=SCHEMA_VERSION,
            component_count=component_count,
            created_at=datetime.now(UTC).isoformat(),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def write_manifest(index_dir: Path, component_count: int) -> Manifest:
    index_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest.create(component_count)
    (index_dir / MANIFEST_NAME).write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
    return manifest


def read_manifest(index_dir: Path) -> Manifest | None:
    """Load an index's manifest, or None for a pre-manifest (old) index."""
    path = index_dir / MANIFEST_NAME
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return Manifest(
        cgir_version=str(data.get("cgir_version", "unknown")),
        schema_version=str(data.get("schema_version", "unknown")),
        component_count=int(data.get("component_count", 0)),
        created_at=str(data.get("created_at", "")),
    )


def compatibility_warning(old: Manifest | None, new: Manifest | None) -> str | None:
    """A human-readable warning if two indexes may not be safely comparable."""
    old_schema = old.schema_version if old else "unknown"
    new_schema = new.schema_version if new else "unknown"
    if old_schema != new_schema:
        return (
            f"warning: schema version mismatch ({old_schema} vs {new_schema}); "
            "diff may be unreliable — rescan both with the same cgir version"
        )
    return None
