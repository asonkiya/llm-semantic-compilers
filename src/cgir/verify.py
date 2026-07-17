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
from cgir.ir.component_spec import ComponentSpec
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
    incremental: bool = True,
) -> VerifyResult:
    """Contract-check a candidate. Incremental by default: only the spliced
    file is re-analyzed and the effect closure re-run over the merged graph
    (equivalence with the full path is pinned by tests). ``run_tests`` (or
    any incremental failure) takes the full-shadow path."""
    old_specs = read_specs(index_dir)
    if component_id not in {s.id for s in old_specs}:
        raise KeyError(component_id)
    node = _find_node(index_dir, component_id)
    if node is None or node.path is None or node.start_line is None or node.end_line is None:
        raise KeyError(f"{component_id}: no source span in index")

    if incremental and not run_tests:
        try:
            new_specs = _incremental_new_specs(index_dir, repo, node, candidate)
        except Exception:
            new_specs = None  # anything unexpected -> full-shadow fallback
        if new_specs is not None:
            return _contract_result(old_specs, new_specs, component_id, fail_on)

    shadow = Path(tempfile.mkdtemp(prefix="cgir-verify-"))
    try:
        repo = repo.resolve()
        shutil.copytree(repo, shadow / "repo", ignore=_IGNORE, dirs_exist_ok=True)
        target_file = shadow / "repo" / node.path
        _splice(target_file, node.start_line, node.end_line, candidate)

        new_index = shadow / "idx"
        scan_repo(shadow / "repo", new_index)
        new_specs = read_specs(new_index)

        tests_ran: list[str] = []
        tests_ok: bool | None = None
        detail = ""
        if run_tests:
            tests_ran = _linked_tests(shadow / "repo", component_id)
            if tests_ran:
                tests_ok, detail = _run_tests(shadow / "repo", tests_ran)

        return _contract_result(
            old_specs, new_specs, component_id, fail_on, tests_ran, tests_ok, detail
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


def _contract_result(
    old_specs: list[ComponentSpec],
    new_specs: list[ComponentSpec],
    component_id: str,
    fail_on: list[str] | None,
    tests_ran: list[str] | None = None,
    tests_ok: bool | None = None,
    detail: str = "",
) -> VerifyResult:
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
    return VerifyResult(
        component_id=component_id,
        contract_ok=not drift,
        violations=viol,
        drift=drift,
        tests_ran=tests_ran or [],
        tests_ok=tests_ok,
        detail=detail,
    )


def _incremental_new_specs(
    index_dir: Path, repo: Path, node: Node, candidate: str
) -> list[ComponentSpec]:
    """Re-analyze ONLY the spliced file against the indexed graph.

    Mechanics: load the old graph from the index; write the spliced file
    into a sparse shadow (one file, no copytree); re-ingest just that file;
    swap its subgraph in (cross-file in-edges preserved by node id); rebuild
    symbol tables (pure graph op); re-run call graph + CFG scoped to the
    file; merge old direct effects (reconstructed from specs: lexical_effects
    marks provenance) with the file's fresh ones; re-run the GLOBAL
    transitive closure so cross-file calls_effectful drift matches a full
    rescan. Equivalence is pinned by tests/unit/test_incremental_verify.py.
    """
    import json

    from cgir.analyses.call_graph import build_call_graph
    from cgir.analyses.cfg import build as build_cfg
    from cgir.analyses.effects import (
        TRANSITIVE_TAG,
        _direct_effects_confidence,
        _module_alias_maps,
        transitive_close,
    )
    from cgir.analyses.purity import score
    from cgir.analyses.symbols import build_symbol_tables, module_of
    from cgir.ir.edges import Edge, EdgeKind
    from cgir.languages.cache import SourceCache
    from cgir.slicing import slice_components
    from cgir.sources import TreeSitterSource

    rel = node.path
    assert rel is not None and node.start_line is not None and node.end_line is not None
    graph = RepoGraph.from_jsonable(json.loads((index_dir / "repo_graph.json").read_text()))
    old_specs = read_specs(index_dir)

    shadow = Path(tempfile.mkdtemp(prefix="cgir-iverify-"))
    try:
        target_file = shadow / rel
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo / rel, target_file)
        _splice(target_file, node.start_line, node.end_line, candidate)

        mini = TreeSitterSource().ingest(shadow)

        # swap the file's subgraph, preserving cross-file in-edges by id
        file_node_ids = [n.id for n in graph.nodes() if n.path == rel]
        preserved: list[Edge] = []
        for nid in file_node_ids:
            for e in graph.in_edges(nid):
                src = graph.get_node(e.src)
                if src.path != rel:
                    preserved.append(e)
        repo_node = next(n for n in graph.nodes(NodeKind.Repository))
        for nid in file_node_ids:
            graph.remove_node(nid)

        mini_data = mini.to_jsonable()
        mini_repo_id = next(n.id for n in mini.nodes(NodeKind.Repository))
        for n in mini.nodes():
            if n.kind is not NodeKind.Repository:
                graph.add_node(n)
        for e in mini_data["edges"]:
            src_id: str = repo_node.id if e["src"] == mini_repo_id else e["src"]
            if src_id == repo_node.id and e["kind"] != EdgeKind.CONTAINS.value:
                continue
            graph.add_edge(
                Edge(src=src_id, dst=e["dst"], kind=EdgeKind(e["kind"]), attrs=e.get("attrs") or {})
            )
        for e in preserved:
            if graph.has_node(e.src) and graph.has_node(e.dst):
                graph.add_edge(e)

        tables = build_symbol_tables(graph)
        build_call_graph(graph, tables, shadow, only_paths={rel})
        build_cfg(graph, shadow, only_paths={rel})

        # direct effects: fresh for the file, reconstructed for the rest
        qual_to_id = {
            str(n.attrs.get("qualname") or n.name): n.id
            for n in graph.nodes()
            if n.kind in {NodeKind.Function, NodeKind.Method}
        }
        conf: dict[str, dict[str, str]] = {}
        for spec in old_specs:
            spec_nid = qual_to_id.get(spec.id)
            if spec_nid is None:
                continue
            conf[spec_nid] = {
                tag: ("lexical" if tag in spec.lexical_effects else "high")
                for tag in spec.effects
                if tag != TRANSITIVE_TAG
            }
        cache = SourceCache(shadow)
        aliases_by_module = _module_alias_maps(graph)
        for n in graph.nodes():
            if n.kind not in {NodeKind.Function, NodeKind.Method} or n.path != rel:
                continue
            module_id = module_of(graph, n)
            aliases = aliases_by_module.get(module_id or "", {})
            conf[n.id] = _direct_effects_confidence(cache, n, aliases)

        effects_map, lexical = transitive_close(graph, conf)
        purity = score(graph, effects_map)
        return slice_components(
            graph, effects=effects_map, purity_scores=purity, lexical_effects=lexical
        )
    finally:
        shutil.rmtree(shadow, ignore_errors=True)
