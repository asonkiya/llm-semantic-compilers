"""cgir verify — the trust loop for an LLM-written component.

Splice a candidate implementation into a *copy* of the repo at the
component's span, rescan, and contract-diff the result against the indexed
spec. Answers the question no compiler or test suite answers directly:
**did this rewrite change what the component is** — its effects, purity,
kind, call surface, entrypoint? Optionally also runs the component's
linked tests (behavior on top of contract).

Deterministic and offline. This is what lets an agent (or a CI gate) trust
a generated change before it lands.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cgir.export.json_export import read_specs
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import Node, NodeKind
from cgir.pipeline import scan_repo
from cgir.report.diff import compute_diff, violations

_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".cgir", "outputs", "site"
)


@dataclass(slots=True)
class VerifyResult:
    component_id: str
    contract_ok: bool
    violations: list[str] = field(default_factory=list)
    drift: dict[str, Any] = field(default_factory=dict)
    tests_ran: list[str] = field(default_factory=list)
    tests_ok: bool | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify(
    index_dir: Path,
    component_id: str,
    candidate: str,
    repo: Path,
    fail_on: list[str] | None = None,
    run_tests: bool = False,
) -> VerifyResult:
    old_specs = read_specs(index_dir)
    if component_id not in {s.id for s in old_specs}:
        raise KeyError(component_id)
    node = _find_node(index_dir, component_id)
    if node is None or node.path is None or node.start_line is None or node.end_line is None:
        raise KeyError(f"{component_id}: no source span in index")

    shadow = Path(tempfile.mkdtemp(prefix="cgir-verify-"))
    try:
        repo = repo.resolve()
        shutil.copytree(repo, shadow / "repo", ignore=_IGNORE, dirs_exist_ok=True)
        target_file = shadow / "repo" / node.path
        _splice(target_file, node.start_line, node.end_line, candidate)

        new_index = shadow / "idx"
        scan_repo(shadow / "repo", new_index)
        new_specs = read_specs(new_index)

        diff = compute_diff(old_specs, new_specs)
        drift: dict[str, Any] = next(
            (c["fields"] for c in diff["changed"] if c["id"] == component_id), {}
        )
        target_diff = {
            "changed": [c for c in diff["changed"] if c["id"] == component_id],
            "entrypoints": {
                key: [e for e in items if e.get("id") == component_id]
                for key, items in diff["entrypoints"].items()
            },
        }
        viol = violations(target_diff, fail_on or [])
        # Pin invariants on the candidate are always enforced.
        from cgir.report.pins import change_violations, state_violations

        old_target = [s for s in old_specs if s.id == component_id]
        new_target = [s for s in new_specs if s.id == component_id]
        viol += change_violations(old_target, new_target)
        viol += [v for v in state_violations(new_specs) if v.startswith(component_id)]

        tests_ran: list[str] = []
        tests_ok: bool | None = None
        detail = ""
        if run_tests:
            tests_ran = _linked_tests(shadow / "repo", component_id)
            if tests_ran:
                tests_ok, detail = _run_tests(shadow / "repo", tests_ran)

        return VerifyResult(
            component_id=component_id,
            contract_ok=not drift,
            violations=viol,
            drift=drift,
            tests_ran=tests_ran,
            tests_ok=tests_ok,
            detail=detail,
        )
    finally:
        shutil.rmtree(shadow, ignore_errors=True)


def _find_node(index_dir: Path, component_id: str) -> Node | None:
    graph_path = index_dir / "repo_graph.json"
    if not graph_path.exists():
        return None
    import json

    graph = RepoGraph.from_jsonable(json.loads(graph_path.read_text()))
    for node in graph.nodes():
        if node.kind in {NodeKind.Function, NodeKind.Method} and (
            node.attrs.get("qualname") == component_id
        ):
            return node
    return None


def _splice(target_file: Path, start_line: int, end_line: int, candidate: str) -> None:
    lines = target_file.read_text().splitlines()
    original_def = lines[start_line - 1] if start_line - 1 < len(lines) else ""
    new_lines = _reindent(candidate, original_def).rstrip("\n").splitlines()
    spliced = lines[: start_line - 1] + new_lines + lines[end_line:]
    target_file.write_text("\n".join(spliced) + "\n")


def _reindent(candidate: str, expected_def: str) -> str:
    body = candidate.rstrip("\n").splitlines()
    if not body:
        return candidate
    got = _leading_ws(body[0])
    want = _leading_ws(expected_def)
    if got == want:
        return candidate
    out = [want + line[len(got) :] if line.startswith(got) else line for line in body]
    return "\n".join(out)


def _leading_ws(line: str) -> str:
    match = re.match(r"\s*", line)
    return match.group(0) if match else ""


def _linked_tests(repo: Path, component_id: str) -> list[str]:
    """Test files that name the component (grep fallback until Sprint 25 linkage)."""
    name = component_id.split(".")[-1]
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    hits: list[str] = []
    for test_dir in (repo / "tests", repo):
        if not test_dir.is_dir():
            continue
        for path in sorted(test_dir.glob("test_*.py")):
            if pattern.search(path.read_text(errors="replace")):
                hits.append(str(path.relative_to(repo)))
    return list(dict.fromkeys(hits))


def _run_tests(repo: Path, test_files: list[str]) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", *test_files],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=300,
    )
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-5:])
    return proc.returncode == 0, tail
