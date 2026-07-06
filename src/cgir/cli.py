"""CLI entry point — matches the command shape in Code-IR.md §Analysis/workflow.

The scan pipeline itself lives in :mod:`cgir.pipeline`; this module (and the
HTTP API) are thin surfaces over it.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Annotated, Any

import typer

from cgir.analyses import param_flow
from cgir.export import graphml as graphml_export
from cgir.export import html_viz
from cgir.export.json_export import read_specs
from cgir.export.mermaid import render_call_graph
from cgir.ir.component_spec import ComponentSpec
from cgir.ir.graph import RepoGraph
from cgir.ir.nodes import NodeKind
from cgir.manifest import compatibility_warning, read_manifest
from cgir.pipeline import scan_repo
from cgir.regenerate import regenerate as run_regenerate
from cgir.report.diff import compute_diff, render_diff, render_diff_markdown, violations
from cgir.report.flow import render_flow
from cgir.report.pack import build_pack, render_pack
from cgir.report.stats import compute_stats, render_text
from cgir.trace import TraceMap

app = typer.Typer(
    add_completion=False,
    help="CodeGraph IR - semantic IR for repo-scale LLM rewriting.",
)


def _version_callback(value: bool) -> None:
    if value:
        from cgir import __version__

        typer.echo(f"cgir {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """CodeGraph IR — semantic IR for repo-scale LLM rewriting."""


@app.command()
def scan(
    repo: Annotated[Path, typer.Argument(exists=True, file_okay=False, dir_okay=True)],
    out: Annotated[
        Path | None, typer.Option("--out", help="Output directory (default <repo>/.cgir)")
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude",
            help="Additional directory names to skip during ingest (repeatable).",
        ),
    ] = None,
) -> None:
    """Scan a repository and write the RepoGraph + ComponentSpec index."""
    result = scan_repo(repo, out, exclude)
    typer.echo(f"Wrote {len(result.specs)} components to {result.out_dir}")
    _print_kind_histogram(result.specs)


def _print_kind_histogram(specs: list[ComponentSpec]) -> None:
    if not specs:
        return
    counts = Counter(spec.kind.value for spec in specs)
    # Stable display order: pure → orchestrator → state → effect → unknown.
    order = ["pure_function", "orchestrator", "state_transformer", "effect_adapter", "unknown"]
    for kind in order:
        if counts.get(kind):
            typer.echo(f"  {kind}: {counts[kind]}")
    for kind, n in counts.items():
        if kind not in order:
            typer.echo(f"  {kind}: {n}")


@app.command()
def export(
    fmt: Annotated[str, typer.Option("--format", help="One of: json | graphml | neo4j")] = "json",
    out: Annotated[Path, typer.Option("--out")] = Path(".cgir"),
) -> None:
    """Re-export an existing index."""
    if fmt == "json":
        typer.echo(f"JSON outputs already at {out}; nothing to do.")
        return
    if fmt == "graphml":
        path = graphml_export.write(out, _load_graph(out))
        typer.echo(f"Wrote {path}")
        return
    if fmt == "neo4j":
        raise NotImplementedError("milestone: P2-neo4j")
    raise typer.BadParameter(f"Unknown format: {fmt}")


@app.command()
def viz(
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
    fmt: Annotated[str, typer.Option("--format", help="One of: html | mermaid")] = "html",
) -> None:
    """Render the component graph — a self-contained HTML page or Mermaid text."""
    specs = _load_specs(index_dir)
    if fmt == "html":
        path = html_viz.write(index_dir, specs, arg_flows=_arg_flows(index_dir))
        typer.echo(f"Wrote {path} — open it in a browser.")
    elif fmt == "mermaid":
        typer.echo(render_call_graph(specs), nl=False)
    else:
        raise typer.BadParameter(f"Unknown format: {fmt}")


@app.command()
def stats(
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Summarize the scanned codebase: kinds, purity, effects, hotspots."""
    result = compute_stats(_load_specs(index_dir))
    if as_json:
        typer.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        typer.echo(render_text(result), nl=False)


@app.command()
def flow(
    component_id: Annotated[str, typer.Argument(metavar="ID")],
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
    depth: Annotated[int, typer.Option("--depth", help="Max hops in each direction.")] = 3,
) -> None:
    """Trace a component: upstream callers, downstream callees, constructed types."""
    specs = _load_specs(index_dir)
    try:
        typer.echo(render_flow(specs, component_id, depth), nl=False)
    except KeyError as exc:
        raise typer.BadParameter(f"Unknown component: {component_id}") from exc


@app.command()
def pack(
    component_id: Annotated[str, typer.Argument(metavar="ID")],
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Repo root, for embedding the target's source."),
    ] = None,
    budget: Annotated[int, typer.Option("--budget", help="Approximate token budget.")] = 4000,
) -> None:
    """Emit the minimal context bundle for working on one component."""
    from cgir.report.pack import referenced_type_names

    specs = _load_specs(index_dir)
    target = next((s for s in specs if s.id == component_id), None)
    if target is None:
        raise typer.BadParameter(f"Unknown component: {component_id}")

    graph = _load_graph(index_dir) if (index_dir / "repo_graph.json").exists() else None
    source = _component_source(graph, component_id, repo) if repo else None
    types = _type_sources(graph, referenced_type_names(target), repo) if repo else {}
    bundle = build_pack(specs, component_id, source=source, budget=budget, types=types)
    typer.echo(render_pack(bundle), nl=False)


def _component_source(graph: RepoGraph | None, component_id: str, repo: Path | None) -> str | None:
    """The target's source lines, via the graph's span (best-effort)."""
    if graph is None or repo is None:
        return None
    for node in graph.nodes():
        if node.kind not in {NodeKind.Function, NodeKind.Method}:
            continue
        if node.attrs.get("qualname") != component_id:
            continue
        return _span_source(node, repo)
    return None


def _type_sources(
    graph: RepoGraph | None, type_names: set[str], repo: Path | None
) -> dict[str, str]:
    """Resolve referenced type names to their in-repo Class definitions."""
    if graph is None or repo is None or not type_names:
        return {}
    out: dict[str, str] = {}
    for node in graph.nodes():
        if node.kind not in {NodeKind.Class, NodeKind.Variable}:
            continue
        if node.name not in type_names or node.name in out:
            continue  # first match wins; ambiguous names take one
        src = _span_source(node, repo)
        if src:
            out[node.name] = src
    return out


def _span_source(node: Any, repo: Path) -> str | None:
    if node.path is None or node.start_line is None or node.end_line is None:
        return None
    try:
        all_lines = (repo / node.path).read_text().splitlines()
    except OSError:
        return None
    return "\n".join(all_lines[node.start_line - 1 : node.end_line]) + "\n"


@app.command()
def mcp(
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
) -> None:
    """Serve the index to agents over MCP (stdio; requires cgir[mcp])."""
    from cgir.api.mcp_server import create_server

    try:
        server = create_server(index_dir)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    server.run()


@app.command()
def diff(
    old_index: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    new_index: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
    markdown: Annotated[
        bool, typer.Option("--markdown", help="Emit a PR-comment-ready markdown report.")
    ] = False,
    fail_on: Annotated[
        list[str] | None,
        typer.Option(
            "--fail-on",
            help="Exit 1 on drift: effect-gain[:tag] | purity-drop | kind-change | "
            "entrypoint-added | entrypoint-change (repeatable).",
        ),
    ] = None,
) -> None:
    """Compare two scan indexes: added/removed components and contract drift."""
    warning = compatibility_warning(read_manifest(old_index), read_manifest(new_index))
    result = compute_diff(_load_specs(old_index), _load_specs(new_index))
    found = violations(result, list(fail_on or []))
    if as_json:
        payload = dict(result)
        if warning:
            payload["warning"] = warning
        payload["violations"] = found
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    elif markdown:
        typer.echo(render_diff_markdown(result, violations=found, warning=warning), nl=False)
    else:
        if warning:
            typer.echo(warning)
        typer.echo(render_diff(result), nl=False)
        if found:
            typer.echo("")
            typer.echo(f"drift violations ({len(found)}):")
            for line in found:
                typer.echo(f"  ! {line}")
    if found:
        raise typer.Exit(code=1)


def _load_graph(index_dir: Path) -> RepoGraph:
    graph_path = index_dir / "repo_graph.json"
    if not graph_path.exists():
        raise typer.BadParameter(f"No graph at {graph_path}; run `cgir scan` first")
    return RepoGraph.from_jsonable(json.loads(graph_path.read_text()))


def _arg_flows(index_dir: Path) -> dict[str, list[dict[str, object]]] | None:
    """PDG-derived param→callee flows, re-keyed by spec id (qualname)."""
    graph_path = index_dir / "repo_graph.json"
    if not graph_path.exists():
        return None
    graph = RepoGraph.from_jsonable(json.loads(graph_path.read_text()))
    flows = param_flow.compute(graph)

    def qual(node_id: str) -> str:
        node = graph.get_node(node_id)
        q = node.attrs.get("qualname") if node.attrs else None
        return str(q) if isinstance(q, str) else node.name

    return {
        qual(caller): [
            {"callee": qual(str(entry["callee"])), "params": entry["params"]} for entry in entries
        ]
        for caller, entries in flows.items()
    }


def _load_specs(index_dir: Path) -> list[ComponentSpec]:
    if not (index_dir / "components").is_dir():
        raise typer.BadParameter(f"No components at {index_dir}; run `cgir scan` first")
    return read_specs(index_dir)


@app.command()
def component(
    component_id: Annotated[str, typer.Argument()],
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
) -> None:
    """Pretty-print a ComponentSpec."""
    spec_path = index_dir / "components" / f"{component_id}.json"
    if not spec_path.exists():
        raise typer.BadParameter(f"No spec at {spec_path}")
    typer.echo(spec_path.read_text())


@app.command()
def trace(
    location: Annotated[str, typer.Argument(help="path:line, e.g. pricing.py:1")],
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
) -> None:
    """Look up which ComponentSpec owns a given source location."""
    if ":" not in location:
        raise typer.BadParameter("location must be <path>:<line>")
    path, line_str = location.rsplit(":", 1)
    line = int(line_str)
    trace_path = index_dir / "trace_map.json"
    if not trace_path.exists():
        raise typer.BadParameter(f"No trace map at {trace_path}; run `cgir scan` first")
    trace_map = TraceMap.read(trace_path)
    hit = trace_map.lookup(path, line)
    if hit is None:
        typer.echo("(no component covers that location)")
        raise typer.Exit(code=1)
    typer.echo(hit)


@app.command(name="regenerate")
def regenerate_cmd(
    component_id: Annotated[str, typer.Argument(metavar="ID")],
    lang: Annotated[str, typer.Option("--lang")] = "typescript",
    index_dir: Annotated[Path, typer.Option("--index")] = Path(".cgir"),
    live: Annotated[
        bool,
        typer.Option("--live", help="Call the Anthropic API (requires cgir[llm] + API key)."),
    ] = False,
) -> None:
    """Print the prompt-pack for a component; --live generates real code."""
    spec_path = index_dir / "components" / f"{component_id}.json"
    if not spec_path.exists():
        raise typer.BadParameter(f"No spec at {spec_path}")
    spec = ComponentSpec.from_dict(json.loads(spec_path.read_text()))
    generator = None
    if live:
        from cgir.regenerate.regenerator import anthropic_generator

        try:
            generator = anthropic_generator()
        except RuntimeError as exc:
            raise typer.BadParameter(str(exc)) from exc
    result = run_regenerate(spec, lang, generator=generator)
    typer.echo("--- PROMPT ---")
    typer.echo(result.prompt)
    typer.echo("--- OUTPUT (live) ---" if result.live else "--- OUTPUT (dry run) ---")
    typer.echo(result.code)


if __name__ == "__main__":  # pragma: no cover
    app()
