"""Watch mode — keep the ``.cgir`` index live as you (or your agent) edit.

The whole local loop (``pack`` → edit → ``impact`` → ``hook``) reads a
``.cgir`` index; a stale index feeds stale context and wrong verdicts.
``cgir watch`` polls the repo, and on a real content change re-scans and
prints the **contract drift** against the previous index — so "you just
made this pure function hit the network" shows up a second after you save,
not in CI.

Change detection is by content hash (``source_hashes``), so editor noise
and non-source edits are free no-ops. Re-scan cost is amortised by the
process-wide parse cache in :mod:`cgir.languages.cache` (unchanged files
are never re-parsed). Per-component incremental *analysis* — recomputing
only the changed blast radius — is the next step; the machinery for it is
:func:`cgir.report.impact.compute_typed_impact`.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cgir.export.json_export import read_specs
from cgir.languages import ADAPTERS
from cgir.pipeline import scan_repo
from cgir.report.diff import compute_diff
from cgir.sources.tree_sitter_source import DEFAULT_IGNORE_DIRS

_SUPPORTED_EXTS: frozenset[str] = frozenset(
    ext for adapter in ADAPTERS.values() for ext in adapter.file_extensions
)
_MANIFEST_NAME = "file_hashes.json"


def _skip(rel: Path, extra_ignore: frozenset[str]) -> bool:
    return any(
        part.startswith(".") or part in DEFAULT_IGNORE_DIRS or part in extra_ignore
        for part in rel.parts
    )


def source_hashes(repo: Path, extra_ignore: frozenset[str] = frozenset()) -> dict[str, str]:
    """Map repo-relative path -> sha256 for every ingestible source file."""
    repo = repo.resolve()
    out: dict[str, str] = {}
    for ext in _SUPPORTED_EXTS:
        for path in repo.rglob(f"*{ext}"):
            rel = path.relative_to(repo)
            if _skip(rel, extra_ignore):
                continue
            try:
                out[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
    return out


def diff_hashes(old: dict[str, str], new: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """(changed, added, deleted) relative paths."""
    changed = sorted(k for k in old.keys() & new.keys() if old[k] != new[k])
    added = sorted(new.keys() - old.keys())
    deleted = sorted(old.keys() - new.keys())
    return changed, added, deleted


def read_manifest(index_dir: Path) -> dict[str, str]:
    path = index_dir / _MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_manifest(index_dir: Path, hashes: dict[str, str]) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / _MANIFEST_NAME).write_text(json.dumps(hashes, indent=2, sort_keys=True))


@dataclass
class Tick:
    changed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    reindexed: bool = False
    components: int = 0
    drift: dict[str, Any] | None = None
    elapsed_ms: float = 0.0


def tick(repo: Path, index_dir: Path, prev_hashes: dict[str, str]) -> tuple[Tick, dict[str, str]]:
    """One iteration: detect content changes and, if any, re-scan + diff."""
    now = source_hashes(repo)
    changed, added, deleted = diff_hashes(prev_hashes, now)
    if not (changed or added or deleted):
        return Tick(), now

    old_specs = read_specs(index_dir) if (index_dir / "components").exists() else []
    start = time.perf_counter()
    result = scan_repo(repo, out=index_dir)
    elapsed = (time.perf_counter() - start) * 1000
    write_manifest(index_dir, now)
    drift = compute_diff(old_specs, result.specs)
    return (
        Tick(
            changed=changed,
            added=added,
            deleted=deleted,
            reindexed=True,
            components=len(result.specs),
            drift=drift,
            elapsed_ms=elapsed,
        ),
        now,
    )


def render_tick(t: Tick) -> str:
    n = len(t.changed) + len(t.added) + len(t.deleted)
    head = f"↻ {n} file(s) → {t.components} components in {t.elapsed_ms:.0f}ms"
    lines = [head]
    drift = t.drift or {}
    changed = drift.get("changed", [])
    if changed:
        lines.append(f"  contract drift ({len(changed)}):")
        for c in changed:
            fields = ", ".join(f"{name} {v['old']}→{v['new']}" for name, v in c["fields"].items())
            lines.append(f"    ~ {c['id']}: {fields}")
    else:
        lines.append("  no contract drift")
    return "\n".join(lines) + "\n"


def run_watch(
    repo: Path,
    index_dir: Path,
    interval: float = 0.5,
    once: bool = False,
    emit: Any = print,
) -> None:
    """Poll ``repo`` and keep ``index_dir`` fresh, printing drift on change."""
    prev = read_manifest(index_dir) or source_hashes(repo)
    # Ensure an index exists so the first real edit can diff against it.
    if not (index_dir / "components").exists():
        scan_repo(repo, out=index_dir)
        write_manifest(index_dir, prev)
    while True:
        result, prev = tick(repo, index_dir, prev)
        if result.reindexed:
            emit(render_tick(result).rstrip("\n"))
        if once:
            return
        time.sleep(interval)


__all__ = [
    "Tick",
    "diff_hashes",
    "read_manifest",
    "render_tick",
    "run_watch",
    "source_hashes",
    "tick",
    "write_manifest",
]
