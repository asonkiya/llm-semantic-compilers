"""CLI entry point — matches the command shape in Code-IR.md §Analysis/workflow.

The scan pipeline itself lives in :mod:`cgir.pipeline`; this module (and the
HTTP API) are thin surfaces over it.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer

from cgir.export import graphml as graphml_export
from cgir.export import html_viz
from cgir.export.json_export import read_specs
from cgir.export.mermaid import render_call_graph
from cgir.ir.component_spec import ComponentSpec
from cgir.ir.graph import RepoGraph
from cgir.pipeline import scan_repo
from cgir.regenerate import regenerate as run_regenerate
from cgir.report.stats import compute_stats, render_text
from cgir.trace import TraceMap

app = typer.Typer(
    add_completion=False,
    help="CodeGraph IR - semantic IR for repo-scale LLM rewriting.",
)


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
        path = html_viz.write(index_dir, specs)
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


def _load_graph(index_dir: Path) -> RepoGraph:
    graph_path = index_dir / "repo_graph.json"
    if not graph_path.exists():
        raise typer.BadParameter(f"No graph at {graph_path}; run `cgir scan` first")
    return RepoGraph.from_jsonable(json.loads(graph_path.read_text()))


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
